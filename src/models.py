import dgl.nn.pytorch as dglnn
import torch
import torch.nn as nn
from dgl import function as fn
from dgl._ffi.base import DGLError
from dgl.nn.pytorch.utils import Identity
from dgl.ops import edge_softmax
from dgl.utils import expand_as_pair


class Bias(nn.Module):
    def __init__(self, size):
        super().__init__()
        self.bias = nn.Parameter(torch.Tensor(size))

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.zeros_(self.bias)

    def forward(self, x):
        return x + self.bias


class GCN(nn.Module):
    def __init__(self, in_feats, n_hidden, n_classes, n_layers, activation, dropout, use_linear):
        super().__init__()
        self.n_layers = n_layers
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.use_linear = use_linear

        self.convs = nn.ModuleList()
        if use_linear:
            self.linear = nn.ModuleList()
        self.bns = nn.ModuleList()

        for i in range(n_layers):
            in_hidden = n_hidden if i > 0 else in_feats
            out_hidden = n_hidden if i < n_layers - 1 else n_classes
            bias = i == n_layers - 1

            self.convs.append(dglnn.GraphConv(in_hidden, out_hidden, "both", bias=bias))
            if use_linear:
                self.linear.append(nn.Linear(in_hidden, out_hidden, bias=False))
            if i < n_layers - 1:
                self.bns.append(nn.BatchNorm1d(out_hidden))

        self.dropout0 = nn.Dropout(min(0.1, dropout))
        self.dropout = nn.Dropout(dropout)
        self.activation = activation

    def forward(self, graph, feat):
        h = feat
        h = self.dropout0(h)

        for i in range(self.n_layers):
            conv = self.convs[i](graph, h)

            if self.use_linear:
                linear = self.linear[i](h)
                h = conv + linear
            else:
                h = conv

            if i < self.n_layers - 1:
                h = self.bns[i](h)
                h = self.activation(h)
                h = self.dropout(h)

        return h


class GATConv(nn.Module):
    def __init__(
        self,
        in_feats,
        out_feats,
        num_heads=1,
        feat_drop=0.0,
        attn_drop=0.0,
        negative_slope=0.2,
        residual=False,
        activation=None,
        allow_zero_in_degree=False,
        norm="none",
    ):
        super(GATConv, self).__init__()
        if norm not in ("none", "both"):
            raise DGLError('Invalid norm value. Must be either "none", "both".' ' But got "{}".'.format(norm))
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self._allow_zero_in_degree = allow_zero_in_degree
        self._norm = norm
        if isinstance(in_feats, tuple):
            self.fc_src = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
            self.fc_dst = nn.Linear(self._in_dst_feats, out_feats * num_heads, bias=False)
        else:
            self.fc = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if residual:
            if self._in_dst_feats != out_feats:
                self.res_fc = nn.Linear(self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = Identity()
        else:
            self.register_buffer("res_fc", None)
        self.reset_parameters()
        self._activation = activation

    def reset_parameters(self):
        gain = nn.init.calculate_gain("relu")
        if hasattr(self, "fc"):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)

    def set_allow_zero_in_degree(self, set_value):
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat):
        with graph.local_scope():
            if not self._allow_zero_in_degree:
                if (graph.in_degrees() == 0).any():
                    assert False

            if isinstance(feat, tuple):
                h_src = self.feat_drop(feat[0])
                h_dst = self.feat_drop(feat[1])
                if not hasattr(self, "fc_src"):
                    self.fc_src, self.fc_dst = self.fc, self.fc
                feat_src, feat_dst = h_src, h_dst
                feat_src = self.fc_src(h_src).view(-1, self._num_heads, self._out_feats)
                feat_dst = self.fc_dst(h_dst).view(-1, self._num_heads, self._out_feats)
            else:
                h_src = h_dst = self.feat_drop(feat)
                feat_src, feat_dst = h_src, h_dst
                feat_src = feat_dst = self.fc(h_src).view(-1, self._num_heads, self._out_feats)
                if graph.is_block:
                    feat_dst = feat_src[: graph.number_of_dst_nodes()]

            if self._norm == "both":
                degs = graph.out_degrees().float().clamp(min=1)
                norm = torch.pow(degs, -0.5)
                shp = norm.shape + (1,) * (feat_src.dim() - 1)
                norm = torch.reshape(norm, shp)
                feat_src = feat_src * norm

            # NOTE: GAT paper uses "first concatenation then linear projection"
            # to compute attention scores, while ours is "first projection then
            # addition", the two approaches are mathematically equivalent:
            # We decompose the weight vector a mentioned in the paper into
            # [a_l || a_r], then
            # a^T [Wh_i || Wh_j] = a_l Wh_i + a_r Wh_j
            # Our implementation is much efficient because we do not need to
            # save [Wh_i || Wh_j] on edges, which is not memory-efficient. Plus,
            # addition could be optimized with DGL's built-in function u_add_v,
            # which further speeds up computation and saves memory footprint.
            el = (feat_src * self.attn_l).sum(dim=-1).unsqueeze(-1)
            er = (feat_dst * self.attn_r).sum(dim=-1).unsqueeze(-1)
            
            # el = 10 * (el - el.min(0, keepdim=True)[0]) / (el.max(0, keepdim=True)[0] - el.min(0, keepdim=True)[0])
            # er = 10 * (er - er.min(0, keepdim=True)[0]) / (er.max(0, keepdim=True)[0] - er.min(0, keepdim=True)[0])
            graph.srcdata.update({"ft": feat_src, "el": el})
            graph.dstdata.update({"er": er})
            # compute edge attention, el and er are a_l Wh_i and a_r Wh_j respectively.
            graph.apply_edges(fn.u_add_v("el", "er", "e"))
            e = self.leaky_relu(graph.edata.pop("e"))
            # compute softmax
            a_dst = edge_softmax(graph, e, norm_by='dst')
            # a_src = edge_softmax(graph, e, norm_by='src')
            # # print(a_dst.max(), a_src.max())
            # a_dst = torch.pow(a_dst + 1e-20, 0.5)
            # a_src = torch.pow(a_src + 1e-20, 0.5)
            graph.edata["a"] = self.attn_drop(a_dst)
            # message passing
            graph.update_all(fn.u_mul_e("ft", "a", "m"), fn.sum("m", "ft"))
            rst = graph.dstdata["ft"]

            if self._norm == "both":
                degs = graph.in_degrees().float().clamp(min=1)
                norm = torch.pow(degs, 0.5)
                shp = norm.shape + (1,) * (feat_dst.dim() - 1)
                norm = torch.reshape(norm, shp)
                rst = rst * norm

            # residual
            if self.res_fc is not None:
                resval = self.res_fc(h_dst).view(h_dst.shape[0], -1, self._out_feats)
                rst = rst + resval
            # activation
            if self._activation is not None:
                rst = self._activation(rst)
            return rst


class GAT(nn.Module):
    def __init__(
        self, in_feats, n_classes, n_hidden, n_layers, n_heads, activation, dropout=0.0, attn_drop=0.0, norm="none"
    ):
        super().__init__()
        self.in_feats = in_feats
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.num_heads = n_heads

        self.convs = nn.ModuleList()
        self.linear = nn.ModuleList()
        self.bns = nn.ModuleList()
        self.biases = nn.ModuleList()

        for i in range(n_layers):
            in_hidden = n_heads * n_hidden if i > 0 else in_feats
            out_hidden = n_hidden if i < n_layers - 1 else n_classes
            # in_channels = n_heads if i > 0 else 1
            out_channels = n_heads

            self.convs.append(GATConv(in_hidden, out_hidden, num_heads=n_heads, attn_drop=attn_drop, norm=norm))

            self.linear.append(nn.Linear(in_hidden, out_channels * out_hidden, bias=False))
            if i < n_layers - 1:
                self.bns.append(nn.BatchNorm1d(out_channels * out_hidden))

        self.bias_last = Bias(n_classes)

        self.dropout0 = nn.Dropout(min(0.1, dropout))
        self.dropout = nn.Dropout(dropout)
        self.activation = activation

    def forward(self, graph, feat):
        h = feat
        h = self.dropout0(h)

        for i in range(self.n_layers):
            conv = self.convs[i](graph, h)
            linear = self.linear[i](h).view(conv.shape)

            h = conv + linear

            if i < self.n_layers - 1:
                h = h.flatten(1)
                h = self.bns[i](h)
                h = self.activation(h)
                h = self.dropout(h)

        h = h.mean(1)
        h = self.bias_last(h)

        return h

class GCNHAConv(nn.Module):
    def __init__(
        self,
        in_feats,
        out_feats,
        K=3,
        num_heads=1,
        feat_drop=0.0,
        attn_drop=0.0,
        negative_slope=0.2,
        residual=False,
        activation=None,
        allow_zero_in_degree=False,
    ):
        super(GCNHAConv, self).__init__()
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self._allow_zero_in_degree = allow_zero_in_degree
        self._K = K

        self.fc = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if residual:
            if self._in_dst_feats != out_feats:
                self.res_fc = nn.Linear(self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = Identity()
        else:
            self.register_buffer("res_fc", None)
        self.reset_parameters()
        self._activation = activation

    def reset_parameters(self):
        gain = nn.init.calculate_gain("relu")
        if hasattr(self, "fc"):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)

    def set_allow_zero_in_degree(self, set_value):
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat):
        with graph.local_scope():
            if not self._allow_zero_in_degree:
                if (graph.in_degrees() == 0).any():
                    assert False
            
            norm = torch.pow(graph.in_degrees().float().clamp(min=1), -0.5)
            shp = norm.shape + (1,) * (feat.dim() - 1)
            norm = torch.reshape(norm, shp).to(feat.device)

            h = self.feat_drop(feat)
            hstack = [h]
            for k in range(self._K):
                rst = hstack[-1] * norm
                graph.ndata['h'] = rst

                graph.update_all(fn.copy_src(src='h', out='m'),
                                 fn.sum(msg='m', out='h'))
                rst = graph.ndata['h']
                rst = rst * norm
                hstack.append(rst)

            feat_src = h
            fstack_dst = hstack
            feat_src = self.fc(feat_src).view(-1, self._num_heads, self._out_feats)
            fstack_dst = [self.fc(feat_dst).view(-1, self._num_heads, self._out_feats) for feat_dst in fstack_dst]
            a_l = (feat_src * self.attn_l).sum(dim=-1).unsqueeze(-1)
            astack_r = [(feat_dst * self.attn_r).sum(dim=-1).unsqueeze(-1) for feat_dst in fstack_dst]
            astack = torch.cat([(a_l + a_r).unsqueeze(-1) for a_r in astack_r], dim=-1)
            a = self.leaky_relu(astack)
            a = torch.nn.functional.softmax(a, dim=-1)
            # compute softmax
            a = self.attn_drop(a)
            rst = 0
            for i in range(a.shape[-1]):
                rst += fstack_dst[i] * a[:, :, :, i]
            # rst = (torch.cat([feat_dst.unsqueeze(-1) for feat_dst in fstack_dst], dim=-1) * a).sum(-1)

            # residual
            if self.res_fc is not None:
                resval = self.res_fc(h_dst).view(h_dst.shape[0], -1, self._out_feats)
                rst = rst + resval
            # activation
            if self._activation is not None:
                rst = self._activation(rst)
            return rst

class GCNHA(nn.Module):
    def __init__(
        self, in_feats, n_classes, n_hidden, n_layers, n_heads, activation, K=3, dropout=0.0, attn_drop=0.0
    ):
        super().__init__()
        self.in_feats = in_feats
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.num_heads = n_heads

        self.convs = nn.ModuleList()
        self.linear = nn.ModuleList()
        self.bns = nn.ModuleList()
        self.biases = nn.ModuleList()

        for i in range(n_layers):
            in_hidden = n_heads * n_hidden if i > 0 else in_feats
            out_hidden = n_hidden if i < n_layers - 1 else n_classes
            # in_channels = n_heads if i > 0 else 1
            out_channels = n_heads

            self.convs.append(GCNHAConv(in_hidden, out_hidden, K=K, num_heads=n_heads, attn_drop=attn_drop))

            self.linear.append(nn.Linear(in_hidden, out_channels * out_hidden, bias=False))
            if i < n_layers - 1:
                self.bns.append(nn.BatchNorm1d(out_channels * out_hidden))

        self.bias_last = Bias(n_classes)

        self.dropout0 = nn.Dropout(min(0.1, dropout))
        self.dropout = nn.Dropout(dropout)
        self.activation = activation

    def forward(self, graph, feat):
        h = feat
        h = self.dropout0(h)

        for i in range(self.n_layers):
            conv = self.convs[i](graph, h)
            linear = self.linear[i](h).view(conv.shape)

            h = conv + linear

            if i < self.n_layers - 1:
                h = h.flatten(1)
                h = self.bns[i](h)
                h = self.activation(h)
                h = self.dropout(h)

        h = h.mean(1)
        h = self.bias_last(h)

        return h

class GATHAConv(nn.Module):
    def __init__(
        self,
        in_feats,
        out_feats,
        K=3,
        num_heads=1,
        feat_drop=0.0,
        attn_drop=0.0,
        negative_slope=0.2,
        residual=False,
        activation=None,
        allow_zero_in_degree=False,
        norm='both'
    ):
        super(GATHAConv, self).__init__()
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self._allow_zero_in_degree = allow_zero_in_degree
        self._K = K
        self._norm = norm

        self.sigma = nn.Parameter(torch.FloatTensor(size=(1,)))
        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.fc = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
        self.hop_attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.hop_attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if residual:
            if self._in_dst_feats != out_feats:
                self.res_fc = nn.Linear(self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = Identity()
        else:
            self.register_buffer("res_fc", None)
        self.reset_parameters()
        self._activation = activation

    def reset_parameters(self):
        gain = nn.init.calculate_gain("relu")
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if hasattr(self, "fc"):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.hop_attn_l, gain=gain)
        nn.init.xavier_normal_(self.hop_attn_r, gain=gain)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)
        nn.init.uniform(self.sigma)

    def set_allow_zero_in_degree(self, set_value):
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat):
        with graph.local_scope():
            if not self._allow_zero_in_degree:
                if (graph.in_degrees() == 0).any():
                    assert False

            h = self.fc(self.feat_drop(feat)).view(-1, self._num_heads, self._out_feats)
            hstack = [h]

            feat_src = h
            
            el = (feat_src * self.attn_l).sum(-1).unsqueeze(-1)
            er = (feat_src * self.attn_r).sum(-1).unsqueeze(-1)
            graph.srcdata.update({"el": el})
            graph.dstdata.update({"er": er})
            # compute edge attention, el and er are a_l Wh_i and a_r Wh_j respectively.
            graph.apply_edges(fn.u_add_v("el", "er", "e"))
            e = self.leaky_relu(graph.edata.pop("e"))
            # compute softmax
            if self._norm in ['both', 'gat']:
                a_src = edge_softmax(graph, e, norm_by='src').clamp(min=1e-10)
                a_dst = edge_softmax(graph, e, norm_by='dst').clamp(min=1e-10)
                
                # # print(a_dst.max(), a_src.max())
                a_dst = torch.pow(a_dst, torch.sigmoid(self.sigma))
                a_src = torch.pow(a_src, 1 - torch.sigmoid(self.sigma))
                graph.edata["a"] = self.attn_drop(a_dst * a_src)
            
            else:
                graph.edata["a"] = self.attn_drop(edge_softmax(graph, e, norm_by='dst'))

            for k in range(self._K):
                
                feat_src = hstack[-1]
                if self._norm in ["both", "gcn"]:
                    degs = graph.out_degrees().float().clamp(min=1)
                    norm = torch.pow(degs, -0.5)
                    shp = norm.shape + (1,) * (feat_src.dim() - 1)
                    norm = torch.reshape(norm, shp)
                    feat_src = feat_src * norm
                graph.srcdata.update({"ft": feat_src})
                # message passing
                graph.update_all(fn.u_mul_e("ft", "a", "m"), fn.sum("m", "ft"))
                feat_src = graph.dstdata["ft"]
                if self._norm in ["both", "gcn"]:
                    degs = graph.in_degrees().float().clamp(min=1)
                    norm = torch.pow(degs, 0.5)
                    shp = norm.shape + (1,) * (feat_src.dim() - 1)
                    norm = torch.reshape(norm, shp)
                    feat_src = feat_src * norm

                hstack.append(feat_src)

            feat_src = h
            fstack_dst = hstack
            feat_src = feat_src.view(-1, self._num_heads, self._out_feats)
            fstack_dst = [feat_dst.view(-1, self._num_heads, self._out_feats) for feat_dst in fstack_dst]
            a_l = (feat_src * self.hop_attn_l).sum(dim=-1).unsqueeze(-1)
            astack_r = [(feat_dst * self.hop_attn_r).sum(dim=-1).unsqueeze(-1) for feat_dst in fstack_dst]
            astack = torch.cat([(a_l + a_r).unsqueeze(-1) for a_r in astack_r], dim=-1)
            a = self.leaky_relu(astack)
            a = torch.nn.functional.softmax(a, dim=-1)
            # compute softmax
            a = self.attn_drop(a)
            rst = 0
            for i in range(a.shape[-1]):
                rst += fstack_dst[i] * a[:, :, :, i]
            # rst = (torch.cat([feat_dst.unsqueeze(-1) for feat_dst in fstack_dst], dim=-1) * a).sum(-1)

            # residual
            if self.res_fc is not None:
                resval = self.res_fc(h).view(h.shape[0], -1, self._out_feats)
                rst = rst + resval
            # activation
            if self._activation is not None:
                rst = self._activation(rst)
            return rst

class GATHA(nn.Module):
    def __init__(
        self, in_feats, n_classes, n_hidden, n_layers, n_heads, activation, K=3, dropout=0.0, feat_drop=0.0, attn_drop=0.0, norm='both'
    ):
        super().__init__()
        self.in_feats = in_feats
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.num_heads = n_heads

        self.convs = nn.ModuleList()
        self.linear = nn.ModuleList()
        self.bns = nn.ModuleList()
        self.biases = nn.ModuleList()

        for i in range(n_layers):
            in_hidden = n_heads * n_hidden if i > 0 else in_feats
            out_hidden = n_hidden if i < n_layers - 1 else n_classes
            # in_channels = n_heads if i > 0 else 1
            out_channels = n_heads

            self.convs.append(GATHAConv(in_hidden, out_hidden, K=K, num_heads=n_heads, feat_drop=feat_drop, attn_drop=attn_drop, norm=norm))

            self.linear.append(nn.Linear(in_hidden, out_channels * out_hidden, bias=False))
            if i < n_layers - 1:
                self.bns.append(nn.BatchNorm1d(out_channels * out_hidden))

        self.bias_last = Bias(n_classes)

        self.dropout0 = nn.Dropout(min(0.1, dropout))
        self.dropout = nn.Dropout(dropout)
        self.activation = activation

    def forward(self, graph, feat):
        h = feat
        h = self.dropout0(h)

        for i in range(self.n_layers):
            conv = self.convs[i](graph, h)
            linear = self.linear[i](h).view(conv.shape)

            h = conv + linear

            if i < self.n_layers - 1:
                h = h.flatten(1)
                h = self.bns[i](h)
                h = self.activation(h)
                h = self.dropout(h)

        h = h.mean(1)
        h = self.bias_last(h)

        return h

class SGATHAConv(nn.Module):
    def __init__(
        self,
        in_feats,
        out_feats,
        K=3,
        num_heads=1,
        feat_drop=0.0,
        attn_drop=0.0,
        negative_slope=0.2,
        residual=False,
        activation=None,
        allow_zero_in_degree=False,
        norm='both'
    ):
        super(SGATHAConv, self).__init__()
        self._num_heads = num_heads
        self._in_src_feats, self._in_dst_feats = expand_as_pair(in_feats)
        self._out_feats = out_feats
        self._allow_zero_in_degree = allow_zero_in_degree
        self._K = K
        self._norm = norm

        self.attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.fc = nn.Linear(self._in_src_feats, out_feats * num_heads, bias=False)
        self.hop_attn_l = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.hop_attn_r = nn.Parameter(torch.FloatTensor(size=(1, num_heads, out_feats)))
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        self.leaky_relu = nn.LeakyReLU(negative_slope)
        if residual:
            if self._in_dst_feats != out_feats:
                self.res_fc = nn.Linear(self._in_dst_feats, num_heads * out_feats, bias=False)
            else:
                self.res_fc = Identity()
        else:
            self.register_buffer("res_fc", None)
        self.reset_parameters()
        self._activation = activation

    def reset_parameters(self):
        gain = nn.init.calculate_gain("relu")
        nn.init.xavier_normal_(self.attn_l, gain=gain)
        nn.init.xavier_normal_(self.attn_r, gain=gain)
        if hasattr(self, "fc"):
            nn.init.xavier_normal_(self.fc.weight, gain=gain)
        else:
            nn.init.xavier_normal_(self.fc_src.weight, gain=gain)
            nn.init.xavier_normal_(self.fc_dst.weight, gain=gain)
        nn.init.xavier_normal_(self.hop_attn_l, gain=gain)
        nn.init.xavier_normal_(self.hop_attn_r, gain=gain)
        if isinstance(self.res_fc, nn.Linear):
            nn.init.xavier_normal_(self.res_fc.weight, gain=gain)

    def set_allow_zero_in_degree(self, set_value):
        self._allow_zero_in_degree = set_value

    def forward(self, graph, feat):
        with graph.local_scope():
            if not self._allow_zero_in_degree:
                if (graph.in_degrees() == 0).any():
                    assert False

            h = self.fc(self.feat_drop(feat)).view(-1, self._num_heads, self._out_feats)
            # hstack = [h]

            feat_src = feat_dst = h
            
            a_l = (feat_src * self.hop_attn_l).sum(dim=-1).unsqueeze(-1)

            el = (feat_src * self.attn_l).sum(-1).unsqueeze(-1)
            er = (feat_src * self.attn_r).sum(-1).unsqueeze(-1)
            graph.srcdata.update({"el": el})
            graph.dstdata.update({"er": er})
            # compute edge attention, el and er are a_l Wh_i and a_r Wh_j respectively.
            graph.apply_edges(fn.u_add_v("el", "er", "e"))
            e = self.leaky_relu(graph.edata.pop("e"))
            # compute softmax
            graph.edata["a"] = self.attn_drop(edge_softmax(graph, e))
            for k in range(self._K+1):
                if k > 0:
                    if self._norm == "both":
                        degs = graph.out_degrees().float().clamp(min=1)
                        norm = torch.pow(degs, -0.5)
                        shp = norm.shape + (1,) * (feat_src.dim() - 1)
                        norm = torch.reshape(norm, shp)
                        feat_src = feat_src * norm
                    graph.srcdata.update({"ft": feat_src})
                    # message passing
                    graph.update_all(fn.u_mul_e("ft", "a", "m"), fn.sum("m", "ft"))
                    feat_src = graph.dstdata["ft"]
                    if self._norm == "both":
                        degs = graph.in_degrees().float().clamp(min=1)
                        norm = torch.pow(degs, 0.5)
                        shp = norm.shape + (1,) * (feat_dst.dim() - 1)
                        norm = torch.reshape(norm, shp)
                        feat_src = feat_src * norm
                # a_r = (feat_src * self.hop_attn_r).sum(dim=-1).unsqueeze(-1)
                # a = torch.cat([(a_l + a_l).unsqueeze(-1).mean(0).unsqueeze(0), (a_l + a_r).unsqueeze(-1).mean(0).unsqueeze(0)], dim=-1)
                # a = self.leaky_relu(a)
                # a = torch.nn.functional.softmax(a, dim=-1)
                # feat_src = h * a[:,:,:,0] + feat_src * a[:,:,:,1]

            # feat_src = h
            # fstack_dst = hstack
            # feat_src = feat_src.view(-1, self._num_heads, self._out_feats)
            # fstack_dst = [feat_dst.view(-1, self._num_heads, self._out_feats) for feat_dst in fstack_dst]
            # a_l = (feat_src * self.hop_attn_l).sum(dim=-1).unsqueeze(-1)
            # astack_r = [(feat_dst * self.hop_attn_r).sum(dim=-1).unsqueeze(-1) for feat_dst in fstack_dst]
            # astack = torch.cat([(a_l + a_r).unsqueeze(-1) for a_r in astack_r], dim=-1)
            # a = self.leaky_relu(astack)
            # a = torch.nn.functional.softmax(a, dim=-1)
            # # compute softmax
            # a = self.attn_drop(a)
            # rst = 0
            # for i in range(a.shape[-1]):
            #     rst += fstack_dst[i] * a[:, :, :, i]
            # rst = (torch.cat([feat_dst.unsqueeze(-1) for feat_dst in fstack_dst], dim=-1) * a).sum(-1)
            # residual
            rst = feat_src
            if self.res_fc is not None:
                resval = self.res_fc(h).view(h.shape[0], -1, self._out_feats)
                rst = rst + resval
            # activation
            if self._activation is not None:
                rst = self._activation(rst)
            return rst

class SGATHA(nn.Module):
    def __init__(
        self, in_feats, n_classes, n_hidden, n_layers, n_heads, activation, K=3, dropout=0.0, attn_drop=0.0, norm='both'
    ):
        super().__init__()
        self.in_feats = in_feats
        self.n_hidden = n_hidden
        self.n_classes = n_classes
        self.n_layers = n_layers
        self.num_heads = n_heads

        self.convs = nn.ModuleList()
        self.linear = nn.ModuleList()
        self.bns = nn.ModuleList()
        self.biases = nn.ModuleList()

        for i in range(n_layers):
            in_hidden = n_heads * n_hidden if i > 0 else in_feats
            out_hidden = n_hidden if i < n_layers - 1 else n_classes
            # in_channels = n_heads if i > 0 else 1
            out_channels = n_heads

            self.convs.append(SGATHAConv(in_hidden, out_hidden, K=K, num_heads=n_heads, attn_drop=attn_drop, norm=norm))

            self.linear.append(nn.Linear(in_hidden, out_channels * out_hidden, bias=False))
            if i < n_layers - 1:
                self.bns.append(nn.BatchNorm1d(out_channels * out_hidden))

        self.bias_last = Bias(n_classes)

        self.dropout0 = nn.Dropout(min(0.1, dropout))
        self.dropout = nn.Dropout(dropout)
        self.activation = activation

    def forward(self, graph, feat):
        h = feat
        h = self.dropout0(h)

        for i in range(self.n_layers):
            conv = self.convs[i](graph, h)
            linear = self.linear[i](h).view(conv.shape)

            h = conv + linear

            if i < self.n_layers - 1:
                h = h.flatten(1)
                h = self.bns[i](h)
                h = self.activation(h)
                h = self.dropout(h)

        h = h.mean(1)
        h = self.bias_last(h)

        return h