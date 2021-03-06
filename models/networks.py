import torch
import torch.nn as nn
from torch.nn import init
import functools
from torch.optim import lr_scheduler
from models.layers.mesh_conv import MeshConv
import torch.nn.functional as F
from models.layers.mesh_pool import MeshPool
from models.layers.mesh_unpool import MeshUnpool
from models.layers.mesh_attention import MeshAttention


###############################################################################
# Helper Functions
###############################################################################


def get_norm_layer(norm_type='instance', num_groups=1):
    if norm_type == 'batch':
        norm_layer = functools.partial(nn.BatchNorm2d, affine=True)
    elif norm_type == 'instance':
        norm_layer = functools.partial(nn.InstanceNorm2d, affine=False)
    elif norm_type == 'group':
        norm_layer = functools.partial(nn.GroupNorm, affine=True, num_groups=num_groups)
    elif norm_type == 'none':
        norm_layer = NoNorm
    else:
        raise NotImplementedError('normalization layer [%s] is not found' % norm_type)
    return norm_layer


def get_norm_args(norm_layer, nfeats_list):
    if hasattr(norm_layer, '__name__') and norm_layer.__name__ == 'NoNorm':
        norm_args = [{'fake': True} for f in nfeats_list]
    elif norm_layer.func.__name__ == 'GroupNorm':
        norm_args = [{'num_channels': f} for f in nfeats_list]
    elif norm_layer.func.__name__ == 'BatchNorm':
        norm_args = [{'num_features': f} for f in nfeats_list]
    else:
        raise NotImplementedError('normalization layer [%s] is not found' % norm_layer.func.__name__)
    return norm_args


class NoNorm(nn.Module):  # todo with abstractclass and pass
    def __init__(self, fake=True):
        self.fake = fake
        super(NoNorm, self).__init__()

    def forward(self, x):
        return x

    def __call__(self, x):
        return self.forward(x)


def get_scheduler(optimizer, opt):
    if opt.lr_policy == 'lambda':
        def lambda_rule(epoch):
            lr_l = 1.0 - max(0, epoch + 1 + opt.epoch_count - opt.niter) / float(opt.niter_decay + 1)
            return lr_l

        scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda_rule)
    elif opt.lr_policy == 'step':
        scheduler = lr_scheduler.StepLR(optimizer, step_size=opt.lr_decay_iters, gamma=0.1)
    elif opt.lr_policy == 'plateau':
        scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.2, threshold=0.01, patience=5)
    else:
        return NotImplementedError('learning rate policy [%s] is not implemented', opt.lr_policy)
    return scheduler


def init_weights(net, init_type, init_gain):
    def init_func(m):
        classname = m.__class__.__name__
        if hasattr(m, 'weight') and (classname.find('Conv') != -1 or classname.find('Linear') != -1):
            if init_type == 'normal':
                init.normal_(m.weight.data, 0.0, init_gain)
            elif init_type == 'xavier':
                init.xavier_normal_(m.weight.data, gain=init_gain)
            elif init_type == 'kaiming':
                init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
            elif init_type == 'orthogonal':
                init.orthogonal_(m.weight.data, gain=init_gain)
            else:
                raise NotImplementedError('initialization method [%s] is not implemented' % init_type)
        elif classname.find('BatchNorm2d') != -1:
            init.normal_(m.weight.data, 1.0, init_gain)
            init.constant_(m.bias.data, 0.0)

    net.apply(init_func)


def init_net(net, init_type, init_gain, gpu_ids):
    if len(gpu_ids) > 0:
        assert (torch.cuda.is_available())
        net.cuda(gpu_ids[0])
        net = net.cuda()
        net = torch.nn.DataParallel(net, gpu_ids)
    if init_type != 'none':
        init_weights(net, init_type, init_gain)
    return net


def define_classifier(input_nc, ncf, ninput_edges, nclasses, opt, gpu_ids, arch, init_type, init_gain):
    net = None
    norm_layer = get_norm_layer(norm_type=opt.norm, num_groups=opt.num_groups)

    if arch == 'mconvnet':
        net = MeshConvNet(norm_layer, input_nc, ncf, nclasses, ninput_edges, opt.pool_res, opt.fc_n,
                          opt.resblocks)
    elif arch == 'meshattentionnet':
        net = MeshAttentionNet(norm_layer, input_nc, ncf, nclasses, ninput_edges, opt.pool_res, opt.fc_n,
                               opt.attn_n_heads, nresblocks=opt.resblocks,
                               attn_max_dist=opt.attn_max_dist,
                               attn_dropout=opt.attn_dropout,
                               prioritize_with_attention=opt.prioritize_with_attention,
                               attn_use_values_as_is=opt.attn_use_values_as_is,
                               double_attention=opt.double_attention,
                               attn_use_positional_encoding=opt.attn_use_positional_encoding,
                               attn_max_relative_position=opt.attn_max_relative_position)
    elif arch == 'meshunet':
        down_convs = [input_nc] + ncf
        up_convs = ncf[::-1] + [nclasses]
        pool_res = [ninput_edges] + opt.pool_res
        net = MeshEncoderDecoder(pool_res, down_convs, up_convs, blocks=opt.resblocks,
                                 transfer_data=True)
    elif arch == 'meshunetwithattention':
        down_convs = [input_nc] + ncf
        up_convs = ncf[::-1] + [nclasses]
        pool_res = [ninput_edges] + opt.pool_res
        net = MeshEncoderDecoderWithAttention(pool_res, down_convs, up_convs,
                                              blocks=opt.resblocks, transfer_data=True,
                                              attn_n_heads=opt.attn_n_heads,
                                              attn_max_dist=opt.attn_max_dist,
                                              attn_dropout=opt.attn_dropout,
                                              prioritize_with_attention=opt.prioritize_with_attention,
                                              attn_use_values_as_is=opt.attn_use_values_as_is,
                                              double_attention=opt.double_attention,
                                              attn_use_positional_encoding=opt.attn_use_positional_encoding,
                                              attn_max_relative_position=opt.attn_max_relative_position)
    else:
        raise NotImplementedError('Encoder model name [%s] is not recognized' % arch)
    return init_net(net, init_type, init_gain, gpu_ids)


def define_loss(opt):
    if opt.dataset_mode == 'classification':
        loss = torch.nn.CrossEntropyLoss()
    elif opt.dataset_mode == 'segmentation':
        loss = torch.nn.CrossEntropyLoss(ignore_index=-1)
    return loss


##############################################################################
# Classes For Classification / Segmentation Networks
##############################################################################

class MeshAttentionNet(nn.Module):
    """Network for learning a global shape descriptor (classification) with global self attention   """

    def __init__(self, norm_layer, nf0, conv_res, nclasses, input_res, pool_res, fc_n,
                 attn_n_heads,  # d_k, d_v,
                 nresblocks=3,
                 attn_max_dist=None,
                 attn_dropout=0.1,
                 prioritize_with_attention=False,
                 attn_use_values_as_is=False,
                 double_attention=False,
                 attn_use_positional_encoding=False,
                 attn_max_relative_position=8):
        super(MeshAttentionNet, self).__init__()
        if double_attention:
            assert attn_use_values_as_is, ("must have attn_use_values_as_is=True if double_attention=True, "
                                           "since the attention layer works on its own outputs")
        self.k = [nf0] + conv_res
        self.res = [input_res] + pool_res
        self.prioritize_with_attention = prioritize_with_attention
        self.double_attention = double_attention
        self.use_values_as_is = attn_use_values_as_is
        norm_args = get_norm_args(norm_layer, self.k[1:])

        for i, ki in enumerate(self.k[:-1]):
            setattr(self, 'conv{}'.format(i), MResConv(ki, self.k[i + 1], nresblocks))
            setattr(self, 'norm{}'.format(i), norm_layer(**norm_args[i]))
            setattr(self, 'attention{}'.format(i), MeshAttention(
                n_head=attn_n_heads, d_model=self.k[i + 1],
                d_k=int(self.k[i + 1] / attn_n_heads), d_v=int(self.k[i + 1] / attn_n_heads),
                attn_max_dist=attn_max_dist, dropout=attn_dropout,
                use_values_as_is=attn_use_values_as_is,
                use_positional_encoding=attn_use_positional_encoding,
                max_relative_position=attn_max_relative_position))
            setattr(self, 'pool{}'.format(i), MeshPool(self.res[i + 1]))

        self.gp = torch.nn.AvgPool1d(self.res[-1])
        # self.gp = torch.nn.MaxPool1d(self.res[-1])
        self.fc1 = nn.Linear(self.k[-1], fc_n)
        self.fc2 = nn.Linear(fc_n, nclasses)

    def forward(self, x, mesh):

        for i in range(len(self.k) - 1):
            x = getattr(self, 'conv{}'.format(i))(x, mesh)
            x = F.relu(getattr(self, 'norm{}'.format(i))(x))
            attention_layer = getattr(self, 'attention{}'.format(i))
            x, attn, attn_per_edge, dist_matrices = attention_layer(x, mesh)
            if self.double_attention:
                _, _, attn_per_edge, _ = attention_layer(x.detach(), mesh, dist_matrices)
            edge_priorities = attn_per_edge if self.prioritize_with_attention else None
            x = getattr(self, 'pool{}'.format(i))(x, mesh, edge_priorities)

        x = self.gp(x)
        x = x.view(-1, self.k[-1])

        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


class MeshConvNet(nn.Module):
    """Network for learning a global shape descriptor (classification)
    """

    def __init__(self, norm_layer, nf0, conv_res, nclasses, input_res, pool_res, fc_n,
                 nresblocks=3):
        super(MeshConvNet, self).__init__()
        self.k = [nf0] + conv_res
        self.res = [input_res] + pool_res
        norm_args = get_norm_args(norm_layer, self.k[1:])

        for i, ki in enumerate(self.k[:-1]):
            setattr(self, 'conv{}'.format(i), MResConv(ki, self.k[i + 1], nresblocks))
            setattr(self, 'norm{}'.format(i), norm_layer(**norm_args[i]))
            setattr(self, 'pool{}'.format(i), MeshPool(self.res[i + 1]))

        self.gp = torch.nn.AvgPool1d(self.res[-1])
        # self.gp = torch.nn.MaxPool1d(self.res[-1])
        self.fc1 = nn.Linear(self.k[-1], fc_n)
        self.fc2 = nn.Linear(fc_n, nclasses)

    def forward(self, x, mesh):

        for i in range(len(self.k) - 1):
            x = getattr(self, 'conv{}'.format(i))(x, mesh)
            x = F.relu(getattr(self, 'norm{}'.format(i))(x))
            x = getattr(self, 'pool{}'.format(i))(x, mesh)

        x = self.gp(x)
        x = x.view(-1, self.k[-1])

        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x


class MResConv(nn.Module):
    def __init__(self, in_channels, out_channels, skips=1):
        super(MResConv, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.skips = skips
        self.conv0 = MeshConv(self.in_channels, self.out_channels, bias=False)
        for i in range(self.skips):
            setattr(self, 'bn{}'.format(i + 1), nn.BatchNorm2d(self.out_channels))
            setattr(self, 'conv{}'.format(i + 1),
                    MeshConv(self.out_channels, self.out_channels, bias=False))

    def forward(self, x, mesh):
        x = self.conv0(x, mesh)
        x1 = x
        for i in range(self.skips):
            x = getattr(self, 'bn{}'.format(i + 1))(F.relu(x))
            x = getattr(self, 'conv{}'.format(i + 1))(x, mesh)
        x += x1
        x = F.relu(x)
        return x


class MeshEncoderDecoder(nn.Module):
    """Network for fully-convolutional tasks (segmentation)
    """

    def __init__(self, pools, down_convs, up_convs, blocks=0, transfer_data=True):
        super(MeshEncoderDecoder, self).__init__()
        self.transfer_data = transfer_data
        self.encoder = MeshEncoder(pools, down_convs, blocks=blocks)
        unrolls = pools[:-1].copy()
        unrolls.reverse()
        self.decoder = MeshDecoder(unrolls, up_convs, blocks=blocks, transfer_data=transfer_data)

    def forward(self, x, meshes):
        fe, before_pool = self.encoder((x, meshes))
        fe = self.decoder((fe, meshes), before_pool)
        return fe

    def __call__(self, x, meshes):
        return self.forward(x, meshes)


class MeshEncoderDecoderWithAttention(nn.Module):
    """ Network for fully-convolutional tasks (segmentation), using attention """

    def __init__(self, pools, down_convs, up_convs, blocks=0, transfer_data=True,
                 attn_n_heads=None,  # d_k, d_v,
                 attn_max_dist=None,
                 attn_dropout=None,
                 prioritize_with_attention=None,
                 attn_use_values_as_is=None,
                 double_attention=None,
                 attn_use_positional_encoding=None,
                 attn_max_relative_position=None):
        super().__init__()
        self.transfer_data = transfer_data
        self.encoder = MeshEncoder(pools, down_convs, blocks=blocks,
                                   attn_n_heads=attn_n_heads,
                                   attn_max_dist=attn_max_dist,
                                   attn_dropout=attn_dropout,
                                   prioritize_with_attention=prioritize_with_attention,
                                   attn_use_values_as_is=attn_use_values_as_is,
                                   double_attention=double_attention,
                                   attn_use_positional_encoding=attn_use_positional_encoding,
                                   attn_max_relative_position=attn_max_relative_position)
        unrolls = pools[:-1].copy()
        unrolls.reverse()
        self.decoder = MeshDecoder(unrolls, up_convs, blocks=blocks, transfer_data=transfer_data)

    def forward(self, x, meshes):
        fe, before_pool = self.encoder((x, meshes))
        fe = self.decoder((fe, meshes), before_pool)
        return fe

    def __call__(self, x, meshes):
        return self.forward(x, meshes)


class DownConv(nn.Module):
    def __init__(self, in_channels, out_channels, blocks=0, pool=0):
        super(DownConv, self).__init__()
        self.bn = []
        self.pool = None
        self.conv1 = MeshConv(in_channels, out_channels)
        self.conv2 = []
        for _ in range(blocks):
            self.conv2.append(MeshConv(out_channels, out_channels))
            self.conv2 = nn.ModuleList(self.conv2)
        for _ in range(blocks + 1):
            self.bn.append(nn.InstanceNorm2d(out_channels))
            self.bn = nn.ModuleList(self.bn)
        if pool:
            self.pool = MeshPool(pool)

    def __call__(self, x):
        return self.forward(x)

    def forward(self, x):
        fe, meshes = x
        x1 = self.conv1(fe, meshes)
        if self.bn:
            x1 = self.bn[0](x1)
        x1 = F.relu(x1)
        x2 = x1
        for idx, conv in enumerate(self.conv2):
            x2 = conv(x1, meshes)
            if self.bn:
                x2 = self.bn[idx + 1](x2)
            x2 = x2 + x1
            x2 = F.relu(x2)
            x1 = x2
        x2 = x2.squeeze(3)
        before_pool = None
        if self.pool:
            before_pool = x2
            x2 = self.pool(x2, meshes)
        return x2, before_pool


class DownConvWithAttention(DownConv):
    def __init__(self, in_channels, out_channels, blocks=0, pool=0,
                 attn_n_heads=4,  # d_k, d_v,
                 attn_max_dist=None,
                 attn_dropout=0.1,
                 prioritize_with_attention=False,
                 attn_use_values_as_is=False,
                 double_attention=False,
                 attn_use_positional_encoding=False,
                 attn_max_relative_position=8):
        super().__init__(in_channels, out_channels, blocks, pool)
        if double_attention:
            assert attn_use_values_as_is, ("must have attn_use_values_as_is=True if double_attention=True, "
                                           "since the attention layer works on its own outputs")
        self.real_pool = self.pool
        self.pool = None
        self.attention = MeshAttention(
            n_head=attn_n_heads, d_model=out_channels,
            d_k=int(out_channels / attn_n_heads), d_v=int(out_channels / attn_n_heads),
            attn_max_dist=attn_max_dist, dropout=attn_dropout,
            use_values_as_is=attn_use_values_as_is,
            use_positional_encoding=attn_use_positional_encoding,
            max_relative_position=attn_max_relative_position)
        self.prioritize_with_attention = prioritize_with_attention
        self.double_attention = double_attention

    def forward(self, x):
        x2, _ = super().forward(x)

        fe, meshes = x
        x2, attn, attn_per_edge, dist_matrices = self.attention(x2, meshes)

        before_pool = None
        if self.real_pool:
            if self.double_attention:
                _, _, attn_per_edge, _ = self.attention(x2.detach(), meshes, dist_matrices)
            edge_priorities = attn_per_edge if self.prioritize_with_attention else None
            before_pool = x2
            x2 = self.real_pool(x2, meshes, edge_priorities)
        return x2, before_pool


class UpConv(nn.Module):
    def __init__(self, in_channels, out_channels, blocks=0, unroll=0, residual=True,
                 batch_norm=True, transfer_data=True):
        super(UpConv, self).__init__()
        self.residual = residual
        self.bn = []
        self.unroll = None
        self.transfer_data = transfer_data
        self.up_conv = MeshConv(in_channels, out_channels)
        if transfer_data:
            self.conv1 = MeshConv(2 * out_channels, out_channels)
        else:
            self.conv1 = MeshConv(out_channels, out_channels)
        self.conv2 = []
        for _ in range(blocks):
            self.conv2.append(MeshConv(out_channels, out_channels))
            self.conv2 = nn.ModuleList(self.conv2)
        if batch_norm:
            for _ in range(blocks + 1):
                self.bn.append(nn.InstanceNorm2d(out_channels))
            self.bn = nn.ModuleList(self.bn)
        if unroll:
            self.unroll = MeshUnpool(unroll)

    def __call__(self, x, from_down=None):
        return self.forward(x, from_down)

    def forward(self, x, from_down):
        from_up, meshes = x
        x1 = self.up_conv(from_up, meshes).squeeze(3)
        if self.unroll:
            x1 = self.unroll(x1, meshes)
        if self.transfer_data:
            x1 = torch.cat((x1, from_down), 1)
        x1 = self.conv1(x1, meshes)
        if self.bn:
            x1 = self.bn[0](x1)
        x1 = F.relu(x1)
        x2 = x1
        for idx, conv in enumerate(self.conv2):
            x2 = conv(x1, meshes)
            if self.bn:
                x2 = self.bn[idx + 1](x2)
            if self.residual:
                x2 = x2 + x1
            x2 = F.relu(x2)
            x1 = x2
        x2 = x2.squeeze(3)
        return x2


class UpConvWithAttention(UpConv):
    def __init__(self, in_channels, out_channels, blocks=0, unroll=0, residual=True,
                 batch_norm=True, transfer_data=True,
                 attn_n_heads=4,  # d_k, d_v,
                 attn_max_dist=None,
                 attn_dropout=0.1):
        super().__init__(in_channels, out_channels, blocks, unroll, residual, batch_norm, transfer_data)
        self.attention = MeshAttention(
            attn_n_heads, out_channels, d_k=int(out_channels / attn_n_heads), d_v=int(out_channels / attn_n_heads),
            attn_max_dist=attn_max_dist, dropout=attn_dropout)

    def forward(self, x, from_down):
        x2 = super().forward(x, from_down)

        from_up, meshes = x
        x2, attn, attn_per_edge = self.attention(x2, meshes)
        return x2


class MeshEncoder(nn.Module):
    def __init__(self, pools, convs, fcs=None, blocks=0, global_pool=None,
                 attn_n_heads=None,  # d_k, d_v,
                 attn_max_dist=None,
                 attn_dropout=None,
                 prioritize_with_attention=None,
                 attn_use_values_as_is=None,
                 double_attention=None,
                 attn_use_positional_encoding=None,
                 attn_max_relative_position=None):
        if attn_n_heads is not None:
            assert (attn_dropout is not None) and (prioritize_with_attention is not None), (
                "Must provide attn_n_heads, attn_dropout and prioritize_with_attention"
                "when using attention for encoder")
        super(MeshEncoder, self).__init__()
        self.fcs = None
        self.convs = []
        for i in range(len(convs) - 1):
            if i + 1 < len(pools):
                pool = pools[i + 1]
            else:
                pool = 0

            if attn_n_heads is None:
                self.convs.append(DownConv(convs[i], convs[i + 1], blocks=blocks, pool=pool))
            else:
                self.convs.append(
                    DownConvWithAttention(convs[i], convs[i + 1], blocks=blocks, pool=pool,
                                          attn_n_heads=attn_n_heads,
                                          attn_max_dist=attn_max_dist,
                                          attn_dropout=attn_dropout,
                                          prioritize_with_attention=prioritize_with_attention,
                                          attn_use_values_as_is=attn_use_values_as_is,
                                          double_attention=double_attention,
                                          attn_use_positional_encoding=attn_use_positional_encoding,
                                          attn_max_relative_position=attn_max_relative_position)
                )

        self.global_pool = None
        if fcs is not None:
            self.fcs = []
            self.fcs_bn = []
            last_length = convs[-1]
            if global_pool is not None:
                if global_pool == 'max':
                    self.global_pool = nn.MaxPool1d(pools[-1])
                elif global_pool == 'avg':
                    self.global_pool = nn.AvgPool1d(pools[-1])
                else:
                    assert False, 'global_pool %s is not defined' % global_pool
            else:
                last_length *= pools[-1]
            if fcs[0] == last_length:
                fcs = fcs[1:]
            for length in fcs:
                self.fcs.append(nn.Linear(last_length, length))
                self.fcs_bn.append(nn.InstanceNorm1d(length))
                last_length = length
            self.fcs = nn.ModuleList(self.fcs)
            self.fcs_bn = nn.ModuleList(self.fcs_bn)
        self.convs = nn.ModuleList(self.convs)
        reset_params(self)

    def forward(self, x):
        fe, meshes = x
        encoder_outs = []
        for conv in self.convs:
            fe, before_pool = conv((fe, meshes))
            encoder_outs.append(before_pool)
        if self.fcs is not None:
            if self.global_pool is not None:
                fe = self.global_pool(fe)
            fe = fe.contiguous().view(fe.size()[0], -1)
            for i in range(len(self.fcs)):
                fe = self.fcs[i](fe)
                if self.fcs_bn:
                    x = fe.unsqueeze(1)
                    fe = self.fcs_bn[i](x).squeeze(1)
                if i < len(self.fcs) - 1:
                    fe = F.relu(fe)
        return fe, encoder_outs

    def __call__(self, x):
        return self.forward(x)


class MeshDecoder(nn.Module):
    def __init__(self, unrolls, convs, blocks=0, batch_norm=True, transfer_data=True):
        super(MeshDecoder, self).__init__()
        self.up_convs = []
        for i in range(len(convs) - 2):
            if i < len(unrolls):
                unroll = unrolls[i]
            else:
                unroll = 0
            self.up_convs.append(UpConv(convs[i], convs[i + 1], blocks=blocks, unroll=unroll,
                                        batch_norm=batch_norm, transfer_data=transfer_data))

        self.final_conv = UpConv(convs[-2], convs[-1], blocks=blocks, unroll=False,
                                 batch_norm=batch_norm, transfer_data=False)
        self.up_convs = nn.ModuleList(self.up_convs)
        reset_params(self)

    def forward(self, x, encoder_outs=None):
        fe, meshes = x
        for i, up_conv in enumerate(self.up_convs):
            before_pool = None
            if encoder_outs is not None:
                before_pool = encoder_outs[-(i + 2)]
            fe = up_conv((fe, meshes), before_pool)
        fe = self.final_conv((fe, meshes))
        return fe

    def __call__(self, x, encoder_outs=None):
        return self.forward(x, encoder_outs)


def reset_params(model):  # todo replace with my init
    for i, m in enumerate(model.modules()):
        weight_init(m)


def weight_init(m):
    if isinstance(m, nn.Conv2d):
        nn.init.xavier_normal_(m.weight)
        nn.init.constant_(m.bias, 0)
