import os
import time
import functools
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from hyperlayers import HyperLinear
from hyperlayers import partialclass
from hyperlayers import HyperLayer

from YUVStyleNet import *
import vgg_net





from torch_scatter import segment_coo

from torch.utils.cpp_extension import load
parent_dir = os.path.dirname(os.path.abspath(__file__))
render_utils_cuda = load(
        name='render_utils_cuda',
        sources=[
            os.path.join(parent_dir, path)
            for path in ['cuda/render_utils.cpp', 'cuda/render_utils_kernel.cu']],
        verbose=True)

total_variation_cuda = load(
        name='total_variation_cuda',
        sources=[
            os.path.join(parent_dir, path)
            for path in ['cuda/total_variation.cpp', 'cuda/total_variation_kernel.cu']],
        verbose=True)

class ConvLayer(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride):
        super(ConvLayer, self).__init__()
        reflection_padding = kernel_size // 2 # same dimension after padding
        self.reflection_pad = nn.ReflectionPad2d(reflection_padding)
        self.conv2d = nn.Conv2d(in_channels, out_channels, kernel_size, stride) # remember this dimension

    def forward(self, x):
        out = self.reflection_pad(x)
        out = self.conv2d(out)
        return out

class HyperFCNet(nn.Module):
    '''Builds a hypernetwork that predicts a fully connected neural network.
    '''
    def __init__(self,
                 in_ch_pos, # MLP input dim (3D points)
                 out_ch, # MLP output dim (rgb)            
                 hyper_in_ch=512, # Hyper input dim (embedding)
                 hyper_num_hidden_layers=1,
                 hyper_hidden_ch=64, # Hyper output dim (hidden)
                 hidden_ch=128, # MLP layer dim
                 num_hidden_layers=6, # Total number of MLP layers
                 ):
        super().__init__()
        self.linear0 = nn.Sequential(nn.Linear(64, 128), nn.ReLU())
        self.linear1 = nn.Sequential(nn.Linear(128, 128), nn.ReLU())
        self.linear2 = nn.Sequential(nn.Linear(256, 128), nn.ReLU())
        self.linear3 = nn.Sequential(nn.Linear(512, 128), nn.ReLU())
        self.linearc = nn.Sequential(nn.Linear(512, 512), nn.ReLU())
        PreconfHyperLinear = partialclass(HyperLinear,
                                          hyper_in_ch=hyper_in_ch,#512
                                          hyper_num_hidden_layers=hyper_num_hidden_layers,#1
                                          hyper_hidden_ch=hyper_hidden_ch)#64
        PreconfHyperLayer = partialclass(HyperLayer,
                                          hyper_in_ch=hyper_in_ch,
                                          hyper_num_hidden_layers=hyper_num_hidden_layers,
                                          hyper_hidden_ch=hyper_hidden_ch)

        self.layers = nn.ModuleList()
        self.layers.append(PreconfHyperLinear(in_ch = in_ch_pos, out_ch=hidden_ch)) # base_remap,128->128
        self.layers.append(PreconfHyperLayer(in_ch = hidden_ch, out_ch=hidden_ch)) # rgb_layer0,128+27->128
        self.layers.append(PreconfHyperLayer(in_ch = hidden_ch, out_ch=hidden_ch)) # rgb_layer1,128->128
        self.layers.append(PreconfHyperLayer(in_ch = hidden_ch, out_ch=hidden_ch // 2)) # rgb_layer2,128->64
        self.layers.append(PreconfHyperLinear(in_ch=hidden_ch // 2, out_ch=out_ch)) # rgb_layer3


    def forward(self, style_f_m0,style_f_m1,style_f_m2,style_f_m3):#([1, 512])
        '''
        :param  style_f_m0,style_f_m1,style_f_m2,style_f_m3: Input to hypernetwork.
        :return: nn.Module; Predicted fully connected neural network.
        '''
        style_f_m0 = self.linear0(style_f_m0)
        style_f_m1 = self.linear1(style_f_m1)
        style_f_m2 = self.linear2(style_f_m2)
        style_f_m3 = self.linear3(style_f_m3)
        style_latent_c = torch.cat((style_f_m0, style_f_m1,style_f_m2,style_f_m3), dim=-1)

        net = []
        for i in range(len(self.layers)):
            net.append(self.layers[i](style_latent_c))

        # print(net)
        return net

def calc_mean_std(feat, eps=1e-5):
    size = feat.size()
    assert (len(size) == 4)
    N, C = size[:2]
    feat_var = feat.view(N, C, -1).var(dim=2) + eps
    feat_std = feat_var.sqrt().view(N, C, 1, 1)
    feat_mean = feat.view(N, C, -1).mean(dim=2).view(N, C, 1, 1)
    return feat_mean, feat_std



def calc_mean_std(feat, eps=1e-5):
    # eps is a small value added to the variance to avoid divide-by-zero. reshape
    size = feat.size()
    assert (len(size) == 4)
    N, C = size[:2]
    # feat_var = feat.view(N, C, -1).var(dim=2) + eps
    feat_var = feat.reshape(N, C, -1).var(dim=2) + eps
    feat_std = feat_var.sqrt().view(N, C, 1, 1)
    # feat_mean = feat.view(N, C, -1).mean(dim=2).view(N, C, 1, 1)
    feat_mean = feat.reshape(N, C, -1).mean(dim=2).view(N, C, 1, 1)
    return feat_mean, feat_std

def mean_variance_norm(feat):
    size = feat.size()
    mean, std = calc_mean_std(feat)
    normalized_feat = (feat - mean.expand(size)) / std.expand(size)
    return normalized_feat
# feature-level AdaIN
class AdaIN(nn.Module):
    def __init__(self):
        super().__init__()
        
    def forward(self, content, style):
        assert (content.size()[:2] == style.size()[:2])
        size = content.size()
        style_mean, style_std = calc_mean_std(style)
        content_mean, content_std = calc_mean_std(content)
        normalized_feat = (content - content_mean.expand(size)) / content_std.expand(size)
        return normalized_feat * style_std.expand(size) + style_mean.expand(size)


def rgb2yuv(rgb):
    # B,_,_,_ = rgb.size()
    rgb_ = rgb.transpose(1,3)                              # input is 3*n*n   default
    A = torch.tensor([[0.299, -0.14714119,0.61497538], 
                      [0.587, -0.28886916, -0.51496512],
                      [0.114, 0.43601035, -0.10001026]]).cuda()   # from  Wikipedia
    yuv = torch.tensordot(rgb_,A,1).transpose(1,3)
    return yuv

def yuv2rgb(yuv):
    yuv_ = yuv.transpose(1,3)                              # input is 3*n*n   default
    A = torch.tensor([[1., 1.,1.], 
                      [0., -0.39465, 2.03211],
                      [1.13983, -0.58060, 0]]).cuda()           # from  Wikipedia
    rgb = torch.tensordot(yuv_,A,1).transpose(1,3)
    return rgb


'''Model'''
class UPST_DirectVoxGO(torch.nn.Module):
    def __init__(self, xyz_min, xyz_max,
                 num_voxels=0, num_voxels_base=0,
                 alpha_init=None,
                 mask_cache_path=None, mask_cache_thres=1e-3,
                 fast_color_thres=0,
                 rgbnet_dim=0, rgbnet_direct=False, rgbnet_full_implicit=False,
                 rgbnet_depth=3, rgbnet_width=128,
                 viewbase_pe=4,
                #  style_net = False,
                 **kwargs):
        super(UPST_DirectVoxGO, self).__init__()



        # VGG
        vgg = vgg_net.vgg
        vgg.load_state_dict(torch.load("./vgg_normalised.pth"))
        self.encoder = vgg_net.Net(vgg)
        # Fixed RAIN_net
        for param in  self.encoder.parameters():
            param.requires_grad = False
        self.encoder.eval()


        self.register_buffer('xyz_min', torch.Tensor(xyz_min))
        self.register_buffer('xyz_max', torch.Tensor(xyz_max))
        self.fast_color_thres = fast_color_thres
        
        self.num_voxels_base = num_voxels_base


        self.voxel_size_base = ((self.xyz_max - self.xyz_min).prod() / self.num_voxels_base).pow(1/3)

        # determine the density bias shift
        self.alpha_init = alpha_init
        self.act_shift = np.log(1/(1-alpha_init) - 1)
        print('dvgo: set density bias shift to', self.act_shift)

        # determine init grid resolution
        self._set_grid_resolution(num_voxels)
        # self._set_grid_resolution(self.num_voxels_base)

        # init density voxel grid
        self.density = torch.nn.Parameter(torch.zeros([1, 1, *self.world_size]))
        
        # init color representation
        self.rgbnet_kwargs = {
            'rgbnet_dim': rgbnet_dim, 'rgbnet_direct': rgbnet_direct,
            'rgbnet_full_implicit': rgbnet_full_implicit,
            'rgbnet_depth': rgbnet_depth, 'rgbnet_width': rgbnet_width,
            'viewbase_pe': viewbase_pe,
        }
        self.rgbnet_full_implicit = rgbnet_full_implicit
        if rgbnet_dim <= 0:
            # color voxel grid  (coarse stage)
            self.k0_dim = 3
            self.k0 = torch.nn.Parameter(torch.zeros([1, self.k0_dim, *self.world_size]))
            self.rgbnet = None
        else:
            # feature voxel grid + shallow MLP  (fine stage)
            if self.rgbnet_full_implicit:
                self.k0_dim = 0
            else:
                self.k0_dim = rgbnet_dim
            self.k0 = torch.nn.Parameter(torch.zeros([1, self.k0_dim, *self.world_size]))
            self.rgbnet_direct = rgbnet_direct
            self.register_buffer('viewfreq', torch.FloatTensor([(2**i) for i in range(viewbase_pe)]))
            dim0 = (3+3*viewbase_pe*2)
            if self.rgbnet_full_implicit:
                pass
            elif rgbnet_direct:
                dim0 += self.k0_dim
            else:
                dim0 += self.k0_dim-3
            

            self.rgbnet = nn.Sequential(#39
                nn.Linear(dim0, rgbnet_width), nn.ReLU(inplace=True),
                *[
                    nn.Sequential(nn.Linear(rgbnet_width, rgbnet_width), nn.ReLU(inplace=True))
                    for _ in range(rgbnet_depth-2)
                ],
                nn.Linear(rgbnet_width, 3),
            )
            nn.init.constant_(self.rgbnet[-1].bias, 0)
            
            self.hfcnet = HyperFCNet(in_ch_pos=dim0,out_ch=dim0,hyper_in_ch=512)
            print("self.hfcnet:",self.hfcnet)
            # nn.init.constant_(self.hfcnet[-1].bias, 0)
            print('dvgo: feature voxel grid', self.k0.shape)#([1, 12, 123, 220, 150])
            print('dvgo: rgbnet', self.rgbnet)#dim_in:39,out:3
        # Using the coarse geometry if provided (used to determine known free space and unknown space)
        # Re-implement as occupancy grid (2021/1/31)
        self.mask_cache_path = mask_cache_path
        self.mask_cache_thres = mask_cache_thres
        if mask_cache_path is not None and mask_cache_path:
            mask_cache = MaskCache(
                    path=mask_cache_path,
                    mask_cache_thres=mask_cache_thres).to(self.xyz_min.device)
            self_grid_xyz = torch.stack(torch.meshgrid(#([123, 220, 150, 3])
                torch.linspace(self.xyz_min[0], self.xyz_max[0], self.density.shape[2]),
                torch.linspace(self.xyz_min[1], self.xyz_max[1], self.density.shape[3]),
                torch.linspace(self.xyz_min[2], self.xyz_max[2], self.density.shape[4]),
            ), -1)
            mask = mask_cache(self_grid_xyz)
        else:
            mask = torch.ones(list(self.world_size), dtype=torch.bool)
        self.mask_cache = MaskCache(
                path=None, mask=mask,
                xyz_min=self.xyz_min, xyz_max=self.xyz_max)
        #增加的部分        
        print('dvgo: feature voxel grid', self.k0.shape)#([1, 12, 123, 220, 150])
        print('dvgo: density voxel grid', self.density.shape)#([1, 12, 123, 220, 150])
    def get_content_feat(self, content_img):
        return self.RAIN_net.get_content_feat(content_img)
    
    def get_style_feat(self, style_img):
        return self.RAIN_net.get_style_feat(style_img)
    def _set_grid_resolution(self, num_voxels):
        # Determine grid resolution
        self.num_voxels = num_voxels#4096000
        self.voxel_size = ((self.xyz_max - self.xyz_min).prod() / num_voxels).pow(1/3)
        self.world_size = ((self.xyz_max - self.xyz_min) / self.voxel_size).long()
        self.voxel_size_ratio = self.voxel_size / self.voxel_size_base
        print('dvgo: voxel_size      ', self.voxel_size)
        print('dvgo: world_size      ', self.world_size)
        print('dvgo: voxel_size_base ', self.voxel_size_base)
        print('dvgo: voxel_size_ratio', self.voxel_size_ratio)

    def get_kwargs(self):
        return {
            'xyz_min': self.xyz_min.cpu().numpy(),
            'xyz_max': self.xyz_max.cpu().numpy(),
            'num_voxels': self.num_voxels,
            'num_voxels_base': self.num_voxels_base,
            'alpha_init': self.alpha_init,
            'act_shift': self.act_shift,
            'voxel_size_ratio': self.voxel_size_ratio,
            'mask_cache_path': self.mask_cache_path,
            'mask_cache_thres': self.mask_cache_thres,
            'fast_color_thres': self.fast_color_thres,
            **self.rgbnet_kwargs,
        }

    @torch.no_grad()
    def maskout_near_cam_vox(self, cam_o, near):
        self_grid_xyz = torch.stack(torch.meshgrid(
            torch.linspace(self.xyz_min[0], self.xyz_max[0], self.density.shape[2]),
            torch.linspace(self.xyz_min[1], self.xyz_max[1], self.density.shape[3]),
            torch.linspace(self.xyz_min[2], self.xyz_max[2], self.density.shape[4]),
        ), -1)
        nearest_dist = torch.stack([
            (self_grid_xyz.unsqueeze(-2) - co).pow(2).sum(-1).sqrt().amin(-1)
            for co in cam_o.split(100)  # for memory saving
        ]).amin(0)
        self.density[nearest_dist[None,None] <= near] = -100

    @torch.no_grad()
    def scale_volume_grid(self, num_voxels,style_net=False):
        print('dvgo: scale_volume_grid start')
        ori_world_size = self.world_size
        self._set_grid_resolution(num_voxels)
        print('dvgo: scale_volume_grid scale world_size from', ori_world_size, 'to', self.world_size)

        self.density = torch.nn.Parameter(
            F.interpolate(self.density.data, size=tuple(self.world_size), mode='trilinear', align_corners=True))
        if self.k0_dim > 0:
            self.k0 = torch.nn.Parameter(
                F.interpolate(self.k0.data, size=tuple(self.world_size), mode='trilinear', align_corners=True))
        else:
            self.k0 = torch.nn.Parameter(torch.zeros([1, self.k0_dim, *self.world_size]))

        mask_cache = MaskCache(
                path=self.mask_cache_path,
                mask_cache_thres=self.mask_cache_thres).to(self.xyz_min.device)
        self_grid_xyz = torch.stack(torch.meshgrid(
            torch.linspace(self.xyz_min[0], self.xyz_max[0], self.density.shape[2]),
            torch.linspace(self.xyz_min[1], self.xyz_max[1], self.density.shape[3]),
            torch.linspace(self.xyz_min[2], self.xyz_max[2], self.density.shape[4]),
        ), -1)
        self_alpha = F.max_pool3d(self.activate_density(self.density), kernel_size=3, padding=1, stride=1)[0,0]
        self.mask_cache = MaskCache(
                path=None, mask=mask_cache(self_grid_xyz) & (self_alpha>self.fast_color_thres),
                xyz_min=self.xyz_min, xyz_max=self.xyz_max)

        print('dvgo: scale_volume_grid finish')
        # if style_net==True:
        #     rgbnet_width = 128
        #     rgbnet_depth = 2
        #     self.style_net = nn.Sequential(nn.Linear(512*16*16, rgbnet_width), nn.ReLU(inplace=True),
        #             *[
        #                 nn.Sequential(nn.Linear(rgbnet_width, rgbnet_width), nn.ReLU(inplace=True))
        #                 for _ in range(rgbnet_depth-2)
        #             ],
        #             nn.Linear(rgbnet_width, self.k0.shape[0]*self.k0.shape[1]*self.k0.shape[2]*self.k0.shape[3]*self.k0.shape[4]),
        #         )
    def voxel_count_views(self, rays_o_tr, rays_d_tr, imsz, near, far, stepsize, downrate=1, irregular_shape=False):
        print('dvgo: voxel_count_views start')
        eps_time = time.time()
        N_samples = int(np.linalg.norm(np.array(self.density.shape[2:])+1) / stepsize) + 1
        rng = torch.arange(N_samples)[None].float()
        count = torch.zeros_like(self.density.detach())
        device = rng.device
        for rays_o_, rays_d_ in zip(rays_o_tr.split(imsz), rays_d_tr.split(imsz)):
            ones = torch.ones_like(self.density).requires_grad_()
            if irregular_shape:
                rays_o_ = rays_o_.split(10000)
                rays_d_ = rays_d_.split(10000)
            else:
                rays_o_ = rays_o_[::downrate, ::downrate].to(device).flatten(0,-2).split(10000)
                rays_d_ = rays_d_[::downrate, ::downrate].to(device).flatten(0,-2).split(10000)

            for rays_o, rays_d in zip(rays_o_, rays_d_):
                vec = torch.where(rays_d==0, torch.full_like(rays_d, 1e-6), rays_d)
                rate_a = (self.xyz_max - rays_o) / vec
                rate_b = (self.xyz_min - rays_o) / vec
                t_min = torch.minimum(rate_a, rate_b).amax(-1).clamp(min=near, max=far)
                t_max = torch.maximum(rate_a, rate_b).amin(-1).clamp(min=near, max=far)
                step = stepsize * self.voxel_size * rng
                interpx = (t_min[...,None] + step/rays_d.norm(dim=-1,keepdim=True))
                rays_pts = rays_o[...,None,:] + rays_d[...,None,:] * interpx[...,None]
                self.grid_sampler(rays_pts, ones).sum().backward()
            with torch.no_grad():
                count += (ones.grad > 1)
        eps_time = time.time() - eps_time
        print('dvgo: voxel_count_views finish (eps time:', eps_time, 'sec)')
        return count

    def density_total_variation_add_grad(self, weight, dense_mode):
        weight = weight * self.world_size.max() / 128
        total_variation_cuda.total_variation_add_grad(
            self.density, self.density.grad, weight, weight, weight, dense_mode)

    def k0_total_variation_add_grad(self, weight, dense_mode):
        weight = weight * self.world_size.max() / 128
        total_variation_cuda.total_variation_add_grad(
            self.k0, self.k0.grad, weight, weight, weight, dense_mode)

    def activate_density(self, density, interval=None):
        interval = interval if interval is not None else self.voxel_size_ratio
        shape = density.shape
        return Raw2Alpha.apply(density.flatten(), self.act_shift, interval).reshape(shape)

    def grid_sampler(self, xyz, *grids, mode=None, align_corners=True):
        '''Wrapper for the interp operation'''
        mode = 'bilinear'
        shape = xyz.shape[:-1]#([478099, 3])->478099
        xyz = xyz.reshape(1,1,1,-1,3)#([478099, 3])->([1, 1, 1, 478099, 3])
        ind_norm = ((xyz - self.xyz_min) / (self.xyz_max - self.xyz_min)).flip((-1,)) * 2 - 1
        ret_lst = [
            # TODO: use `rearrange' to make it readable
            F.grid_sample(grid, ind_norm, mode=mode, align_corners=align_corners).reshape(grid.shape[1],-1).T.reshape(*shape,grid.shape[1])
            for grid in grids
        ]
        for i in range(len(grids)):
            if ret_lst[i].shape[-1] == 1:
                ret_lst[i] = ret_lst[i].squeeze(-1)
        if len(ret_lst) == 1:
            return ret_lst[0]
        return ret_lst

    def hit_coarse_geo(self, rays_o, rays_d, near, far, stepsize, **render_kwargs):
        '''Check whether the rays hit the solved coarse geometry or not'''
        shape = rays_o.shape[:-1]
        rays_o = rays_o.reshape(-1, 3).contiguous()
        rays_d = rays_d.reshape(-1, 3).contiguous()
        stepdist = stepsize * self.voxel_size
        ray_pts, mask_outbbox, ray_id = render_utils_cuda.sample_pts_on_rays(
                rays_o, rays_d, self.xyz_min, self.xyz_max, near, far, stepdist)[:3]
        mask_inbbox = ~mask_outbbox
        hit = torch.zeros([len(rays_o)], dtype=torch.bool)
        hit[ray_id[mask_inbbox][self.mask_cache(ray_pts[mask_inbbox])]] = 1
        return hit.reshape(shape)

    def sample_ray(self, rays_o, rays_d, near, far, stepsize, is_train=False, **render_kwargs):
        '''Sample query points on rays.
        All the output points are sorted from near to far.
        Input:
            rays_o, rayd_d:   both in [N, 3] indicating ray configurations.
            near, far:        the near and far distance of the rays.
            stepsize:         the number of voxels of each sample step.
        Output:
            ray_pts:          [M, 3] storing all the sampled points.
            ray_id:           [M]    the index of the ray of each point.
            step_id:          [M]    the i'th step on a ray of each point.
        '''
        rays_o = rays_o.contiguous()
        rays_d = rays_d.contiguous()
        stepdist = stepsize * self.voxel_size
        ray_pts, mask_outbbox, ray_id, step_id, N_steps, t_min, t_max = render_utils_cuda.sample_pts_on_rays(
            rays_o, rays_d, self.xyz_min, self.xyz_max, near, far, stepdist)
        mask_inbbox = ~mask_outbbox
        ray_pts = ray_pts[mask_inbbox]
        ray_id = ray_id[mask_inbbox]
        step_id = step_id[mask_inbbox]
        return ray_pts, ray_id, step_id

    def forward(self, rays_o, rays_d, viewdirs, style_embedded, global_step=None, **render_kwargs):
        '''Volume rendering
        @rays_o:   [N, 3] the starting point of the N shooting rays.
        @rays_d:   [N, 3] the shooting direction of the N rays.
        @viewdirs: [N, 3] viewing direction to compute positional embedding for MLP.
        '''
        assert len(rays_o.shape)==2 and rays_o.shape[-1]==3, 'Only suuport point queries in [N, 3] format'




        # print('dvgo: feature voxel grid', self.k0.shape)#([1, 12, 123, 220, 150])
        # print('dvgo: density voxel grid', self.density.shape)#([1, 12, 123, 220, 150])

        ret_dict = {}
        N = len(rays_o)

        # sample points on rays采样光线上的点
        ray_pts, ray_id, step_id = self.sample_ray(#([667589, 3])
                rays_o=rays_o, rays_d=rays_d, is_train=global_step is not None, **render_kwargs)
        interval = render_kwargs['stepsize'] * self.voxel_size_ratio

        # skip known free space 跳过已知空的空间
        if self.mask_cache is not None:
            mask = self.mask_cache(ray_pts)#[667589, 3]-》([667589])
            ray_pts = ray_pts[mask]
            ray_id = ray_id[mask]
            step_id = step_id[mask]

        # query for alpha w/ post-activation 查询alpha 后激活
        density = self.grid_sampler(ray_pts, self.density)#self.density([1, 1, 61, 110, 75])-》
        alpha = self.activate_density(density, interval)
        if self.fast_color_thres > 0:
            mask = (alpha > self.fast_color_thres)
            ray_pts = ray_pts[mask]
            ray_id = ray_id[mask]
            step_id = step_id[mask]
            density = density[mask]
            alpha = alpha[mask]

        # compute accumulated transmittance计算累积透过率
        weights, alphainv_last = Alphas2Weights.apply(alpha, ray_id, N)
        if self.fast_color_thres > 0:
            mask = (weights > self.fast_color_thres)
            weights = weights[mask]
            alpha = alpha[mask]
            ray_pts = ray_pts[mask]
            ray_id = ray_id[mask]
            step_id = step_id[mask]

        # query for color查询颜色
        if not self.rgbnet_full_implicit:
            k0 = self.grid_sampler(ray_pts, self.k0)#self.k0[1, 12, 61, 110, 75]——》([1, 12, 123, 222, 149])
            # if (style_embedded!=None) and (self.style_net!=None):
            #     style_feature = style_embedded.reshape(-1)
            #     style_feature = self.style_net(style_feature)
            #     style_feature = style_feature.reshape(self.k0.size()[0],self.k0.size()[1],self.k0.size()[2],self.k0.size()[3],self.k0.size()[4])
            #     k0_temp = self.k0+style_feature
            #     k0 = self.grid_sampler(ray_pts, k0_temp)#self.k0[1, 12, 61, 110, 75]——》
            #     # k0 = self.grid_sampler(ray_pts, self.k0)
            # else:
            #     k0 = self.grid_sampler(ray_pts, self.k0)#self.k0[1, 12, 61, 110, 75]——》
            

        if self.rgbnet is None:
            # no view-depend effect
            rgb = torch.sigmoid(k0)
        else:
            # view-dependent color emission
            if self.rgbnet_direct:
                k0_view = k0
            else:
                k0_view = k0[:, 3:]
                k0_diffuse = k0[:, :3]
            # print(viewdirs.size())
            # print('dvgo: viewdirs', viewdirs.shape)#([1, 12, 123, 220, 150])
            viewdirs_emb = (viewdirs.unsqueeze(-1) * self.viewfreq).flatten(-2)#([10000, 3])->([10000, 12])

            # # print(viewdirs_emb.size())
            viewdirs_emb = torch.cat([viewdirs, viewdirs_emb.sin(), viewdirs_emb.cos()], -1)#viewdirs([10000, 3]),viewdirs_emb[N,12]->([10000, 27])
            viewdirs_emb = viewdirs_emb.flatten(0,-2)[ray_id]#([10000, 27])->([234984, 27])

            if style_embedded==None:
                rgb_feat = torch.cat([k0_view, viewdirs_emb], -1)#([234605, 12])+([234605, 27])->([234605, 39])
                rgb_feat = torch.cat([rgb_feat], -1)
                rgb_logit = self.rgbnet(rgb_feat)#([234605, 39])->torch.Size([234605, 3])
                # print(rgb_logit.size())
                if self.rgbnet_direct:
                    rgb = torch.sigmoid(rgb_logit)
                else:
                    rgb = torch.sigmoid(rgb_logit + k0_diffuse)
            else:
                # style_feature = self.encoder.encode(style_embedded)#([1, 3, 128, 128])->([1, 512, 16, 16])
                # style_feature = self.encoder.encode(style_embedded)
                style_feats = self.encoder.encode_with_intermediate(style_embedded)
                style_fm0,_,_ = self.encoder.calc_feat_mean_std(style_feats[0])
                style_fm1,_,_ = self.encoder.calc_feat_mean_std(style_feats[1])
                style_fm2,_,_ = self.encoder.calc_feat_mean_std(style_feats[2])
                style_fm3,_,_ = self.encoder.calc_feat_mean_std(style_feats[3])

                style_fm0 = style_fm0.clone().detach()
                style_fm1 = style_fm1.clone().detach()
                style_fm2 = style_fm2.clone().detach()
                style_fm3 = style_fm3.clone().detach()

                hyper_rgb = self.hfcnet(style_fm0,style_fm1,style_fm2,style_fm3)#e([1, 131072])->hyper_rgb[3]

                rgb_feat = torch.cat([k0_view, viewdirs_emb], -1)
                rgb_feat = rgb_feat.unsqueeze(0)#12
                rgb_feat = hyper_rgb[0](rgb_feat)
                rgb_feat = hyper_rgb[1](rgb_feat)
                rgb_feat = hyper_rgb[2](rgb_feat)
                rgb_feat = hyper_rgb[3](rgb_feat)
                rgb_feat = hyper_rgb[4](rgb_feat)
                rgb_feat = rgb_feat.squeeze(0)

                rgb_logit = self.rgbnet(rgb_feat)
                if self.rgbnet_direct:
                    rgb = torch.sigmoid(rgb_logit)
                else:
                    rgb = torch.sigmoid(rgb_logit + k0_diffuse)
                

        # Ray marching
        src_temp = weights.unsqueeze(-1) * rgb
        # print(src_temp.size())
        # print(src_temp)
        # print(ray_id.size())
        # print(ray_id)
        rgb_marched = segment_coo(#([8192, 3])
                # src=(weights.unsqueeze(-1) * rgb),
                src=src_temp,
                index=ray_id,
                out=torch.zeros([N, 3]),
                reduce='sum')
        rgb_marched += (alphainv_last.unsqueeze(-1) * render_kwargs['bg'])#([8192, 3])
        
        
        
        ret_dict.update({
            'alphainv_last': alphainv_last,
            'weights': weights,
            'rgb_marched': rgb_marched,
            'raw_alpha': alpha,
            'raw_rgb': rgb,
            'ray_id': ray_id,
        })

        if render_kwargs.get('render_depth', False):
            with torch.no_grad():
                depth = segment_coo(
                        src=(weights * step_id),
                        index=ray_id,
                        out=torch.zeros([N]),
                        reduce='sum')
            ret_dict.update({'depth': depth})

        return ret_dict


''' Module for the searched coarse geometry
It supports query for the known free space and unknown space.
'''
class MaskCache(nn.Module):
    def __init__(self, path=None, mask_cache_thres=None, mask=None, xyz_min=None, xyz_max=None):
        super().__init__()
        if path is not None:
            st = torch.load(path)
            self.mask_cache_thres = mask_cache_thres
            density = F.max_pool3d(st['model_state_dict']['density'], kernel_size=3, padding=1, stride=1)
            alpha = 1 - torch.exp(-F.softplus(density + st['model_kwargs']['act_shift']) * st['model_kwargs']['voxel_size_ratio'])
            mask = (alpha >= self.mask_cache_thres).squeeze(0).squeeze(0)
            xyz_min = torch.Tensor(st['model_kwargs']['xyz_min'])
            xyz_max = torch.Tensor(st['model_kwargs']['xyz_max'])
        else:
            mask = mask.bool()
            xyz_min = torch.Tensor(xyz_min)
            xyz_max = torch.Tensor(xyz_max)

        self.register_buffer('mask', mask)
        xyz_len = xyz_max - xyz_min
        self.register_buffer('xyz2ijk_scale', (torch.Tensor(list(mask.shape)) - 1) / xyz_len)
        self.register_buffer('xyz2ijk_shift', -xyz_min * self.xyz2ijk_scale)

    @torch.no_grad()
    def forward(self, xyz):
        '''Skip know freespace
        @xyz:   [..., 3] the xyz in global coordinate.
        '''
        shape = xyz.shape[:-1]
        xyz = xyz.reshape(-1, 3)
        mask = render_utils_cuda.maskcache_lookup(self.mask, xyz, self.xyz2ijk_scale, self.xyz2ijk_shift)
        mask = mask.reshape(shape)
        return mask


''' Misc
'''
class Raw2Alpha(torch.autograd.Function):
    @staticmethod
    def forward(ctx, density, shift, interval):
        '''
        alpha = 1 - exp(-softplus(density + shift) * interval)
              = 1 - exp(-log(1 + exp(density + shift)) * interval)
              = 1 - exp(log(1 + exp(density + shift)) ^ (-interval))
              = 1 - (1 + exp(density + shift)) ^ (-interval)
        '''
        exp, alpha = render_utils_cuda.raw2alpha(density, shift, interval);
        if density.requires_grad:
            ctx.save_for_backward(exp)
            ctx.interval = interval
        return alpha

    @staticmethod
    @torch.autograd.function.once_differentiable
    def backward(ctx, grad_back):
        '''
        alpha' = interval * ((1 + exp(density + shift)) ^ (-interval-1)) * exp(density + shift)'
               = interval * ((1 + exp(density + shift)) ^ (-interval-1)) * exp(density + shift)
        '''
        exp = ctx.saved_tensors[0]
        interval = ctx.interval
        return render_utils_cuda.raw2alpha_backward(exp, grad_back.contiguous(), interval), None, None

class Alphas2Weights(torch.autograd.Function):
    @staticmethod
    def forward(ctx, alpha, ray_id, N):
        weights, T, alphainv_last, i_start, i_end = render_utils_cuda.alpha2weight(alpha, ray_id, N)
        if alpha.requires_grad:
            ctx.save_for_backward(alpha, weights, T, alphainv_last, i_start, i_end)
            ctx.n_rays = N
        return weights, alphainv_last

    @staticmethod
    @torch.autograd.function.once_differentiable
    def backward(ctx, grad_weights, grad_last):
        alpha, weights, T, alphainv_last, i_start, i_end = ctx.saved_tensors
        grad = render_utils_cuda.alpha2weight_backward(
                alpha, weights, T, alphainv_last,
                i_start, i_end, ctx.n_rays, grad_weights, grad_last)
        return grad, None, None


''' Ray and batch
'''
def get_rays(H, W, K, c2w, inverse_y, flip_x, flip_y, mode='center'):
    i, j = torch.meshgrid(
        torch.linspace(0, W-1, W, device=c2w.device),
        torch.linspace(0, H-1, H, device=c2w.device))  # pytorch's meshgrid has indexing='ij'
    i = i.t().float()
    j = j.t().float()
    if mode == 'lefttop':
        pass
    elif mode == 'center':
        i, j = i+0.5, j+0.5
    elif mode == 'random':
        i = i+torch.rand_like(i)
        j = j+torch.rand_like(j)
    else:
        raise NotImplementedError

    if flip_x:
        i = i.flip((1,))
    if flip_y:
        j = j.flip((0,))
    if inverse_y:
        dirs = torch.stack([(i-K[0][2])/K[0][0], (j-K[1][2])/K[1][1], torch.ones_like(i)], -1)
    else:
        dirs = torch.stack([(i-K[0][2])/K[0][0], -(j-K[1][2])/K[1][1], -torch.ones_like(i)], -1)
    # Rotate ray directions from camera frame to the world frame
    rays_d = torch.sum(dirs[..., np.newaxis, :] * c2w[:3,:3], -1)  # dot product, equals to: [c2w.dot(dir) for dir in dirs]
    # Translate camera frame's origin to the world frame. It is the origin of all rays.
    rays_o = c2w[:3,3].expand(rays_d.shape)
    return rays_o, rays_d


def get_rays_np(H, W, K, c2w):
    i, j = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32), indexing='xy')
    dirs = np.stack([(i-K[0][2])/K[0][0], -(j-K[1][2])/K[1][1], -np.ones_like(i)], -1)
    # Rotate ray directions from camera frame to the world frame
    rays_d = np.sum(dirs[..., np.newaxis, :] * c2w[:3,:3], -1)  # dot product, equals to: [c2w.dot(dir) for dir in dirs]
    # Translate camera frame's origin to the world frame. It is the origin of all rays.
    rays_o = np.broadcast_to(c2w[:3,3], np.shape(rays_d))
    return rays_o, rays_d


def ndc_rays(H, W, focal, near, rays_o, rays_d):
    # Shift ray origins to near plane
    t = -(near + rays_o[...,2]) / rays_d[...,2]
    rays_o = rays_o + t[...,None] * rays_d

    # Projection
    o0 = -1./(W/(2.*focal)) * rays_o[...,0] / rays_o[...,2]
    o1 = -1./(H/(2.*focal)) * rays_o[...,1] / rays_o[...,2]
    o2 = 1. + 2. * near / rays_o[...,2]

    d0 = -1./(W/(2.*focal)) * (rays_d[...,0]/rays_d[...,2] - rays_o[...,0]/rays_o[...,2])
    d1 = -1./(H/(2.*focal)) * (rays_d[...,1]/rays_d[...,2] - rays_o[...,1]/rays_o[...,2])
    d2 = -2. * near / rays_o[...,2]

    rays_o = torch.stack([o0,o1,o2], -1)
    rays_d = torch.stack([d0,d1,d2], -1)

    return rays_o, rays_d


def get_rays_of_a_view(H, W, K, c2w, ndc, inverse_y, flip_x, flip_y, mode='center'):
    rays_o, rays_d = get_rays(H, W, K, c2w, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y, mode=mode)
    viewdirs = rays_d / rays_d.norm(dim=-1, keepdim=True)
    if ndc:
        rays_o, rays_d = ndc_rays(H, W, K[0][0], 1., rays_o, rays_d)
    return rays_o, rays_d, viewdirs


@torch.no_grad()
def get_training_rays(rgb_tr, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_training_rays: start')
    assert len(np.unique(HW, axis=0)) == 1
    assert len(np.unique(Ks.reshape(len(Ks),-1), axis=0)) == 1
    assert len(rgb_tr) == len(train_poses) and len(rgb_tr) == len(Ks) and len(rgb_tr) == len(HW)
    H, W = HW[0]
    K = Ks[0]
    eps_time = time.time()
    rays_o_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)#([100, 800, 800, 3])
    rays_d_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)#([100, 800, 800, 3])
    viewdirs_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)
    imsz = [1] * len(rgb_tr)
    for i, c2w in enumerate(train_poses):
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        rays_o_tr[i].copy_(rays_o.to(rgb_tr.device))
        rays_d_tr[i].copy_(rays_d.to(rgb_tr.device))
        viewdirs_tr[i].copy_(viewdirs.to(rgb_tr.device))
        del rays_o, rays_d, viewdirs
    eps_time = time.time() - eps_time
    print('get_training_rays: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz


@torch.no_grad()
def get_training_rays_irregular_shape(rgb_tr, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_training_rays_irregular_shape: start')
    # assert len(np.unique(HW, axis=0)) == 1
    # assert len(np.unique(Ks.reshape(len(Ks),-1), axis=0)) == 1
    assert len(rgb_tr) == len(train_poses) and len(rgb_tr) == len(Ks) and len(rgb_tr) == len(HW)
    H, W = HW[0]
    K = Ks[0]
    eps_time = time.time()
    rays_o_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)#([100, 800, 800, 3])
    rays_d_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)#([100, 800, 800, 3])
    viewdirs_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)
    imsz = [1] * len(rgb_tr)
    for i, c2w in enumerate(train_poses):
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        rays_o_tr[i].copy_(rays_o.to(rgb_tr.device))
        rays_d_tr[i].copy_(rays_d.to(rgb_tr.device))
        viewdirs_tr[i].copy_(viewdirs.to(rgb_tr.device))
        del rays_o, rays_d, viewdirs
    eps_time = time.time() - eps_time
    print('get_training_rays: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz



@torch.no_grad()#rgb_tr_ori([100, 800, 800, 3]);train_poses([100, 4, 4]);HW(100,2)
def get_training_rays_flatten(rgb_tr_ori, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_training_rays_flatten: start')
    assert len(rgb_tr_ori) == len(train_poses) and len(rgb_tr_ori) == len(Ks) and len(rgb_tr_ori) == len(HW)
    eps_time = time.time()
    DEVICE = rgb_tr_ori[0].device
    N = sum(im.shape[0] * im.shape[1] for im in rgb_tr_ori)#([100, 800, 800, 3])->100*800*800=64000000
    rgb_tr = torch.zeros([N,3], device=DEVICE)#([64000000, 3])
    rays_o_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    rays_d_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    viewdirs_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    imsz = []
    top = 0
    for c2w, img, (H, W), K in zip(train_poses, rgb_tr_ori, HW, Ks):
        assert img.shape[:2] == (H, W)
        rays_o, rays_d, viewdirs = get_rays_of_a_view(#rays_o([800, 800, 3]);rays_d([800, 800, 3]);viewdirs([800, 800, 3])
                H=H, W=W, K=K, c2w=c2w, ndc=ndc,
                inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        n = H * W#640000
        rgb_tr[top:top+n].copy_(img.flatten(0,1))
        rays_o_tr[top:top+n].copy_(rays_o.flatten(0,1).to(DEVICE))
        rays_d_tr[top:top+n].copy_(rays_d.flatten(0,1).to(DEVICE))
        viewdirs_tr[top:top+n].copy_(viewdirs.flatten(0,1).to(DEVICE))
        imsz.append(n)
        top += n

    assert top == N
    eps_time = time.time() - eps_time
    print('get_training_rays_flatten: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz


@torch.no_grad()#rgb_tr_ori([100, 800, 800, 3]);train_poses([100, 4, 4]);HW(100,2)
def get_training_rays_in_maskcache_sampling(rgb_tr_ori, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y, model, render_kwargs):
    print('get_training_rays_in_maskcache_sampling: start')
    assert len(rgb_tr_ori) == len(train_poses) and len(rgb_tr_ori) == len(Ks) and len(rgb_tr_ori) == len(HW)
    CHUNK = 64
    DEVICE = rgb_tr_ori[0].device
    eps_time = time.time()
    N = sum(im.shape[0] * im.shape[1] for im in rgb_tr_ori)#([100, 800, 800, 3])->100*800*800=64000000
    rgb_tr = torch.zeros([N,3], device=DEVICE)#([64000000, 3])
    rays_o_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    rays_d_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    viewdirs_tr = torch.zeros_like(rgb_tr)#([64000000, 3])
    imsz = []
    top = 0
    for c2w, img, (H, W), K in zip(train_poses, rgb_tr_ori, HW, Ks):
        assert img.shape[:2] == (H, W)
        rays_o, rays_d, viewdirs = get_rays_of_a_view(#rays_o([800, 800, 3]);rays_d([800, 800, 3]);viewdirs([800, 800, 3]);
                H=H, W=W, K=K, c2w=c2w, ndc=ndc,
                inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        mask = torch.empty(img.shape[:2], device=DEVICE, dtype=torch.bool)#mask([800, 800])
        for i in range(0, img.shape[0], CHUNK):
            mask[i:i+CHUNK] = model.hit_coarse_geo(
                    rays_o=rays_o[i:i+CHUNK], rays_d=rays_d[i:i+CHUNK], **render_kwargs).to(DEVICE)
        n = mask.sum()
        rgb_tr[top:top+n].copy_(img[mask])
        rays_o_tr[top:top+n].copy_(rays_o[mask].to(DEVICE))
        rays_d_tr[top:top+n].copy_(rays_d[mask].to(DEVICE))
        viewdirs_tr[top:top+n].copy_(viewdirs[mask].to(DEVICE))
        imsz.append(n)
        top += n

    print('get_training_rays_in_maskcache_sampling: ratio', top / N)
    rgb_tr = rgb_tr[:top]
    rays_o_tr = rays_o_tr[:top]
    rays_d_tr = rays_d_tr[:top]
    viewdirs_tr = viewdirs_tr[:top]
    eps_time = time.time() - eps_time
    print('get_training_rays_in_maskcache_sampling: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz


def batch_indices_generator(N, BS):
    # torch.randperm on cuda produce incorrect results in my machine
    idx, top = torch.LongTensor(np.random.permutation(N)), 0
    while True:
        if top + BS > N:
            idx, top = torch.LongTensor(np.random.permutation(N)), 0
        yield idx[top:top+BS]
        top += BS

