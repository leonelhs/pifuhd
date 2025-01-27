# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.

import os
import cv2
import numpy as np
import torch
from numpy.linalg import inv
from tqdm import tqdm

from PIFuHD.geometry import index
from PIFuHD.mesh_util import save_obj_mesh_with_color, reconstruction
from PIFuHD.model import HGPIFuNetwNML, HGPIFuMRNet


class Reconstructor:

    def __init__(self, opt):
        # load checkpoints
        state_dict_path = None
        if opt.load_netMR_checkpoint_path is not None:
            state_dict_path = opt.load_netMR_checkpoint_path
        elif opt.resume_epoch < 0:
            state_dict_path = '%s/%s_train_latest' % (opt.checkpoints_path, opt.name)
            opt.resume_epoch = 0
        else:
            state_dict_path = '%s/%s_train_epoch_%d' % (opt.checkpoints_path, opt.name, opt.resume_epoch)

        self.start_id = opt.start_id
        self.end_id = opt.end_id

        self.range = range(opt.start_id, opt.end_id)

        self.cuda = torch.device('cuda:%d' % opt.gpu_id if torch.cuda.is_available() else 'cpu')

        state_dict = None
        if state_dict_path is not None and os.path.exists(state_dict_path):
            print('Resuming from ', state_dict_path)
            state_dict = torch.load(state_dict_path, map_location=self.cuda)
            print('Warning: opt is overwritten.')
            dataroot = opt.dataroot
            resolution = opt.resolution
            results_path = opt.results_path
            loadSize = opt.loadSize

            opt = state_dict['opt']
            opt.dataroot = dataroot
            opt.resolution = resolution
            opt.results_path = results_path
            opt.loadSize = loadSize
        else:
            exit(f'failed loading state dict! {state_dict_path}')

        opt_netG = state_dict['opt_netG']
        self.netG = HGPIFuNetwNML(opt_netG, 'orthogonal').to(device=self.cuda)
        self.netMR = HGPIFuMRNet(opt, self.netG, 'orthogonal').to(device=self.cuda)

        # load checkpoints
        self.netMR.load_state_dict(state_dict['model_state_dict'])

        os.makedirs(opt.checkpoints_path, exist_ok=True)
        os.makedirs(opt.results_path, exist_ok=True)
        os.makedirs('%s/%s/recon' % (opt.results_path, opt.name), exist_ok=True)

        self.opt = opt

    def __set_eval(self):
        self.netG.eval()

    def evaluate(self, test_dataset):
        # test
        if self.start_id < 0:
            self.start_id = 0
        if self.end_id < 0:
            self.end_id = len(test_dataset)

        with torch.no_grad():
            self.__set_eval()

            print('generate mesh (test) ...')
            for i in tqdm(range(self.start_id, self.end_id)):
                if i >= len(test_dataset):
                    break

                # for multi-person processing, set it to False
                if True:
                    test_data = test_dataset[i]

                    save_path = '%s/%s/recon/result_%s_%d.obj' % (
                        self.opt.results_path, self.opt.name, test_data['name'], self.opt.resolution)

                    print(save_path)
                    return self._gen_mesh_gray(self.opt.resolution, test_data, save_path, components=self.opt.use_compose)
                else:
                    for j in range(test_dataset.get_n_person(i)):
                        test_dataset.person_id = j
                        test_data = test_dataset[i]
                        save_path = '%s/%s/recon/result_%s_%d.obj' % (self.opt.results_path, opt.name, test_data['name'], j)
                        return self._gen_mesh_gray(self.opt.resolution, self.cuda, test_data, save_path, components=self.opt.use_compose)

    def _gen_mesh_gray(self, res, data, save_path, thresh=0.5, use_octree=True, components=False):
        image_tensor_global = data['img_512'].to(device=self.cuda)
        image_tensor = data['img'].to(device=self.cuda)
        calib_tensor = data['calib'].to(device=self.cuda)

        self.netMR.filter_global(image_tensor_global)
        self.netMR.filter_local(image_tensor[:, None])

        try:
            if self.netMR.netG.netF is not None:
                image_tensor_global = torch.cat([image_tensor_global, self.netMR.netG.nmlF], 0)
            if self.netMR.netG.netB is not None:
                image_tensor_global = torch.cat([image_tensor_global, self.netMR.netG.nmlB], 0)
        except:
            pass

        b_min = data['b_min']
        b_max = data['b_max']
        try:
            save_img_path = save_path[:-4] + '.png'
            save_img_list = []
            for v in range(image_tensor_global.shape[0]):
                save_img = (np.transpose(image_tensor_global[v].detach().cpu().numpy(), (1, 2, 0)) * 0.5 + 0.5)[:, :,
                           ::-1] * 255.0
                save_img_list.append(save_img)
            save_img = np.concatenate(save_img_list, axis=1)
            cv2.imwrite(save_img_path, save_img)

            verts, faces, _, _ = reconstruction(
                self.netMR, self.cuda, calib_tensor, res, b_min, b_max, thresh, use_octree=use_octree, num_samples=50000)
            verts_tensor = torch.from_numpy(verts.T).unsqueeze(0).to(device=self.cuda).float()
            # if 'calib_world' in data:
            #     calib_world = data['calib_world'].numpy()[0]
            #     verts = np.matmul(np.concatenate([verts, np.ones_like(verts[:,:1])],1), inv(calib_world).T)[:,:3]

            color = np.zeros(verts.shape)
            interval = 50000
            for i in range(len(color) // interval + 1):
                left = i * interval
                if i == len(color) // interval:
                    right = -1
                else:
                    right = (i + 1) * interval
                self.netMR.calc_normal(verts_tensor[:, None, :, left:right], calib_tensor[:, None], calib_tensor)
                nml = self.netMR.nmls.detach().cpu().numpy()[0] * 0.5 + 0.5
                color[left:right] = nml.T

            save_obj_mesh_with_color(save_path, verts, faces, color)
            return save_img_path, save_path
        except Exception as e:
            print(e)

    def _gen_mesh_color(self, res, data, save_path, thresh=0.5, use_octree=True, components=False):
        image_tensor_global = data['img_512'].to(device=self.cuda)
        image_tensor = data['img'].to(device=self.cuda)
        calib_tensor = data['calib'].to(device=self.cuda)

        self.netMR.filter_global(image_tensor_global)
        self.netMR.filter_local(image_tensor[:, None])

        try:
            if self.netMR.netG.netF is not None:
                image_tensor_global = torch.cat([image_tensor_global, self.netMR.netG.nmlF], 0)
            if self.netMR.netG.netB is not None:
                image_tensor_global = torch.cat([image_tensor_global, self.netMR.netG.nmlB], 0)
        except:
            pass

        b_min = data['b_min']
        b_max = data['b_max']
        try:
            save_img_path = save_path[:-4] + '.png'
            save_img_list = []
            for v in range(image_tensor_global.shape[0]):
                save_img = (np.transpose(image_tensor_global[v].detach().cpu().numpy(), (1, 2, 0)) * 0.5 + 0.5)[:, :,
                           ::-1] * 255.0
                save_img_list.append(save_img)
            save_img = np.concatenate(save_img_list, axis=1)
            cv2.imwrite(save_img_path, save_img)

            verts, faces, _, _ = reconstruction(
                self.netMR, self.cuda, calib_tensor, res, b_min, b_max, thresh, use_octree=use_octree, num_samples=100000)
            verts_tensor = torch.from_numpy(verts.T).unsqueeze(0).to(device=self.cuda).float()

            # if this returns error, projection must be defined somewhere else
            xyz_tensor = self.netMR.projection(verts_tensor, calib_tensor[:1])
            uv = xyz_tensor[:, :2, :]
            color = index(image_tensor[:1], uv).detach().cpu().numpy()[0].T
            color = color * 0.5 + 0.5

            if 'calib_world' in data:
                calib_world = data['calib_world'].numpy()[0]
                verts = np.matmul(np.concatenate([verts, np.ones_like(verts[:, :1])], 1), inv(calib_world).T)[:, :3]

            save_obj_mesh_with_color(save_path, verts, faces, color)

        except Exception as e:
            print(e)
