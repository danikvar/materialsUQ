import torch
from torch import Tensor
import torch.nn.functional as F
from torch.nn import Sequential, Linear, BatchNorm1d
import torch_geometric
from torch_geometric.nn import (
    Set2Set,
    global_mean_pool,
    global_add_pool,
    global_max_pool,
    CGConv,
    DiffGroupNorm
)
from torch_scatter import scatter_mean, scatter_add, scatter_max, scatter
import warnings
from .BNNLayer import BNNLayer

# CGCNN
class CGCNN_bbp(torch.nn.Module):
    def __init__(
        self,
        data,
        dim1=64,
        dim2=64,
        pre_fc_count=1,
        gc_count=3,
        post_fc_count=1,
        pool="global_mean_pool",
        pool_order="early",
        batch_norm="True",
        batch_track_stats="True",
        act="relu",
        dropout_rate=0.0,
        **kwargs
    ):
        super(CGCNN_bbp, self).__init__()
        
        if batch_track_stats == "False":
            self.batch_track_stats = False 
        else:
            self.batch_track_stats = True 
        self.batch_norm = batch_norm
        self.pool = pool
        self.act = act
        self.pool_order = pool_order
        self.dropout_rate = dropout_rate
        
        ##Determine gc dimension dimension
        assert gc_count > 0, "Need at least 1 GC layer"        
        if pre_fc_count == 0:
            gc_dim = data.num_features
        else:
            gc_dim = dim1
        ##Determine post_fc dimension
        if pre_fc_count == 0:
            post_fc_dim = data.num_features
        else:
            post_fc_dim = dim1
        ##Determine output dimension length
        if data[0].y.ndim == 0:
            output_dim = 1
        else:
            output_dim = len(data[0].y[0])

        ##Set up pre-GNN dense layers (NOTE: in v0.1 this is always set to 1 layer)
        if pre_fc_count > 0:
            self.pre_lin_list = torch.nn.ModuleList()
            for i in range(pre_fc_count):
                if i == 0:
                    lin = torch.nn.Linear(data.num_features, dim1)
                    self.pre_lin_list.append(lin)
                else:
                    lin = torch.nn.Linear(dim1, dim1)
                    self.pre_lin_list.append(lin)
        elif pre_fc_count == 0:
            self.pre_lin_list = torch.nn.ModuleList()

        ##Set up GNN layers
        self.conv_list = torch.nn.ModuleList()
        self.bn_list = torch.nn.ModuleList()
        for i in range(gc_count):
            conv = CGConv(
                gc_dim, data.num_edge_features, aggr="mean", batch_norm=False
            )
            self.conv_list.append(conv)
            ##Track running stats set to false can prevent some instabilities; this causes other issues with different val/test performance from loader size?
            if self.batch_norm == "True":
                #bn = BatchNorm1d(gc_dim, track_running_stats=self.batch_track_stats)
                bn = DiffGroupNorm(gc_dim, 10, track_running_stats=self.batch_track_stats)
                self.bn_list.append(bn)

        ##Set up post-GNN dense layers (NOTE: in v0.1 there was a minimum of 2 dense layers, and fc_count(now post_fc_count) added to this number. In the current version, the minimum is zero)
        if post_fc_count > 0:
            self.post_lin_list = torch.nn.ModuleList()
            for i in range(post_fc_count):
                if i == 0:
                    ##Set2set pooling has doubled dimension
                    if self.pool_order == "early" and self.pool == "set2set":
                        lin = BNNLayer(post_fc_dim * 2, dim2, activation='none', prior_mean=0, prior_rho=0)
                    else:
                        lin = BNNLayer(post_fc_dim, dim2, activation='none', prior_mean=0, prior_rho=0)
                    self.post_lin_list.append(lin)
                else:
                    lin = BNNLayer(dim2, dim2, activation='none', prior_mean=0, prior_rho=0)
                    self.post_lin_list.append(lin)
            self.lin_out = BNNLayer(dim2, output_dim, activation='none', prior_mean=0, prior_rho=0)

        elif post_fc_count == 0:
            self.post_lin_list = torch.nn.ModuleList()
            if self.pool_order == "early" and self.pool == "set2set":
                self.lin_out = BNNLayer(post_fc_dim*2, output_dim, activation='none', prior_mean=0, prior_rho=0)
            else:
                self.lin_out = BNNLayer (post_fc_dim, output_dim, activation='none', prior_mean=0, prior_rho=0)   

        ##Set up set2set pooling (if used)
        ##Should processing_setps be a hypereparameter?
        if self.pool_order == "early" and self.pool == "set2set":
            self.set2set = Set2Set(post_fc_dim, processing_steps=3)
        elif self.pool_order == "late" and self.pool == "set2set":
            self.set2set = Set2Set(output_dim, processing_steps=3, num_layers=1)
            # workaround for doubled dimension by set2set; if late pooling not reccomended to use set2set
            self.lin_out_2 = torch.nn.Linear(output_dim * 2, output_dim)

    def forward(self, data, mymode = 'forward'):
        
        warnings.warn(mymode)
        #warnings.warn("Warning Output: ".join(''.join(out)))
        tkl = 0
        tlklh = 0
        ##Pre-GNN dense layers
        for i in range(0, len(self.pre_lin_list)):
            if i == 0:
                out = self.pre_lin_list[i](data.x)
                out = getattr(F, self.act)(out)
                #prev_out = out
            else:
                out = self.pre_lin_list[i](out)
                out = getattr(F, self.act)(out)
                #out = torch.add(out, prev_out)
                #prev_out = out
        prev_out = out

        ##GNN layers
        for i in range(0, len(self.conv_list)):
            if len(self.pre_lin_list) == 0 and i == 0:
                if self.batch_norm == "True":
                    out = self.conv_list[i](data.x, data.edge_index, data.edge_attr)
                    out = self.bn_list[i](out)
                else:
                    out = self.conv_list[i](data.x, data.edge_index, data.edge_attr)
            else:
                if self.batch_norm == "True":
                    out = self.conv_list[i](out, data.edge_index, data.edge_attr)
                    out = self.bn_list[i](out)
                else:
                    out = self.conv_list[i](out, data.edge_index, data.edge_attr)            
            #out = getattr(F, self.act)(out)
            out = torch.add(out, prev_out)
            out = F.dropout(out, p=self.dropout_rate, training=self.training)
            prev_out = out

        if mymode == 'forward':
            ##Post-GNN dense layers
            if self.pool_order == "early":
                if self.pool == "set2set":
                    out, kl = self.set2set(out, data.batch)
                    tkl += kl
                else:
                    out = getattr(torch_geometric.nn, self.pool)(out, data.batch)
                    #tkl += kl
                for i in range(0, len(self.post_lin_list)):
                    out, kl = self.post_lin_list[i](out, mode = mymode)
                    tkl += kl
                    out = getattr(F, self.act)(out)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                out,kl = self.lin_out(out, mode = mymode)
                tkl += kl
                #out = torch.add(out, prev_out)
                #prev_out = out

            elif self.pool_order == "late":
                for i in range(0, len(self.post_lin_list)):
                    out, kl = self.post_lin_list[i](out, mode = mymode)
                    tkl += kl
                    out = getattr(F, self.act)(out)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                out, kl = self.lin_out(out, mode = mymode)
                tkl += kl
                #out = torch.add(out, prev_out)
                #prev_out = out

                if self.pool == "set2set":
                    out, kl = self.set2set(out, data.batch)
                    tkl += kl
                    out = self.lin_out_2(out, mode = mymode)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                else:
                    out,kl = getattr(torch_geometric.nn, self.pool)(out, data.batch)
                    tkl += kl

            if out.shape[1] == 1:
                return out.view(-1), tkl
            else:
                return out, tkl
        else:
            ##Post-GNN dense layers
            if self.pool_order == "early":
                if self.pool == "set2set":
                    out = self.set2set(out, data.batch)
                else:
                    out = getattr(torch_geometric.nn, self.pool)(out, data.batch)
                    #tkl += kl
                for i in range(0, len(self.post_lin_list)):
                    out = self.post_lin_list[i](out, mode = mymode)
                    warnings.warn(str(len(out)))
                    out = getattr(F, self.act)(out)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                out = self.lin_out(out, mode = mymode)
                #out = torch.add(out, prev_out)
                #prev_out = out

            elif self.pool_order == "late":
                for i in range(0, len(self.post_lin_list)):
                    out = self.post_lin_list[i](out, mode = mymode)
                    out = getattr(F, self.act)(out)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                out = self.lin_out(out, mode = mymode)
                #out = torch.add(out, prev_out)
                #prev_out = out

                if self.pool == "set2set":
                    out = self.set2set(out, data.batch)
                    out = self.lin_out_2(out, mode = mymode)
                    #out = torch.add(out, prev_out)
                    #prev_out = out
                else:
                    out = getattr(torch_geometric.nn, self.pool)(out, data.batch)

            if out.shape[1] == 1:
                return out.view(-1)
            else:
                return out

    @staticmethod
    def loss_fn(kl, lklh, n_batch):
        return (kl / n_batch - lklh).mean()