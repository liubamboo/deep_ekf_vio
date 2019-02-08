import os
import glob
import pandas as pd
import numpy as np
import torch
import se3_math
from PIL import Image
from torch.utils.data import Dataset
from log import logger
from torchvision import transforms
import time
from params import par


def get_data_info(sequences, seq_len, overlap, sample_times=1):
    subseq_image_path_list = []
    subseq_len_list = []
    subseq_type_list = []
    subseq_seq_list = []
    subseq_id_list = []
    subseq_gt_pose_list = []

    for seq in sequences:
        start_t = time.time()
        gt_poses = np.load(os.path.join(par.pose_dir, seq + ".npy"))
        fpaths = sorted(glob.glob(os.path.join(par.image_dir, seq, "*.png")))
        assert (len(gt_poses) == len(fpaths))  # make sure the number of images corresponds to number of poses

        if sample_times > 1:
            sample_interval = int(np.ceil(seq_len / sample_times))
            start_frames = list(range(0, seq_len, sample_interval))
            print('Sample start from frame {}'.format(start_frames))
        else:
            start_frames = [0]

        for st in start_frames:
            jump = seq_len - overlap

            # The original image and data
            subseq_image_path, subseq_gt_pose, subseq_ids = [], [], []
            for i in range(st, len(fpaths), jump):
                if i + seq_len <= len(fpaths):  # this will discard a few frames at the end
                    subseq_image_path.append(fpaths[i:i + seq_len])
                    subseq_gt_pose.append(gt_poses[i:i + seq_len])
                    # first index is the start, second is where the next sub-sequence start
                    subseq_ids.append(np.array([i, i + jump]))

            subseq_type = ["normal"] * len(subseq_image_path)
            subseq_seq = [seq] * len(subseq_image_path)

            # TODO Mirrors and going in reverse

            subseq_gt_pose_list += subseq_gt_pose
            subseq_image_path_list += subseq_image_path
            subseq_len_list += [len(xs) for xs in subseq_image_path]
            subseq_seq_list += subseq_seq
            subseq_type_list += subseq_type
            subseq_id_list += subseq_ids

            # ensure all sequence length are the same
            assert (subseq_len_list.count(seq_len) == len(subseq_len_list))
        print('Folder {} finish in {} sec'.format(seq, time.time() - start_t))

    # Convert to pandas dataframes
    data = {'seq_len': subseq_len_list, 'image_path': subseq_image_path_list, "seq": subseq_seq_list,
            "type": subseq_type_list, "id": subseq_id_list, 'pose': subseq_gt_pose_list}
    return pd.DataFrame(data, columns=data.keys())


class ImageSequenceDataset(Dataset):
    def __init__(self, info_dataframe, new_sizeize=None, img_mean=None, img_std=(1, 1, 1),
                 minus_point_5=False, training=True):

        # Transforms
        self.pre_runtime_transformer = transforms.Compose([
            transforms.Resize((new_sizeize[0], new_sizeize[1]))
        ])

        if training:
            transform_ops = []
            if par.data_aug_rand_color.enable:
                transform_ops.append(transforms.ColorJitter(**par.data_aug_rand_color.params))
            transform_ops.append(transforms.ToTensor())
            self.runtime_transformer = transforms.Compose(transform_ops)
        else:
            self.runtime_transformer = transforms.ToTensor()

        # Normalization
        self.minus_point_5 = minus_point_5
        self.normalizer = transforms.Normalize(mean=img_mean, std=img_std)

        # log
        logger.print("Transform parameters: ")
        logger.print("pre_runtime_transformer:", self.pre_runtime_transformer)
        logger.print("runtime_transformer:", self.runtime_transformer)
        logger.print("minus_point_5:", self.minus_point_5)
        logger.print("normalizer:", self.normalizer)

        # organize data
        self.data_info = info_dataframe
        self.subseq_len_list = list(self.data_info.seq_len)
        self.subseq_image_path_list = np.asarray(self.data_info.image_path)  # image paths
        self.subseq_gt_pose_list = np.asarray(self.data_info.pose)
        self.subseq_type_list = np.asarray(self.data_info.type)
        self.subseq_seq_list = np.asarray(self.data_info.seq)
        self.subseq_id_list = np.asarray(self.data_info.id)

        self.image_cache = {}
        total_images = len(self.subseq_image_path_list[0]) * len(self.subseq_image_path_list)
        counter = 0
        start_t = time.time()
        for subseq_image_path in self.subseq_image_path_list:
            for path in subseq_image_path:
                if path not in self.image_cache:
                    self.image_cache[path] = self.pre_runtime_transformer(Image.open(path))
                counter += 1
                print("Processed %d/%d (%.2f%%)" % (counter, total_images, counter / total_images * 100), end="\r")
        logger.print("Image preprocessing took %.2fs" % (time.time() - start_t))

    def __getitem__(self, index):
        gt_poses = self.subseq_gt_pose_list[index]
        type = self.subseq_type_list[index]
        seq = self.subseq_seq_list[index]
        id = self.subseq_id_list[index]
        # transform
        gt_rel_poses = []
        for i in range(1, len(gt_poses)):
            T_i_vkm1 = gt_poses[i - 1]
            T_i_vk = gt_poses[i]
            T_vkm1_k = se3_math.reorthogonalize_SE3(np.linalg.inv(T_i_vkm1).dot(T_i_vk))
            r_vk_vkm1_vkm1 = T_vkm1_k[0:3, 3]  # get the translation from T
            phi_vkm1_vk = se3_math.log_SO3(T_vkm1_k[0:3, 0:3])
            gt_rel_poses.append(np.concatenate([r_vk_vkm1_vkm1, phi_vkm1_vk, ]))

        gt_rel_poses = torch.FloatTensor(gt_rel_poses)

        image_paths = self.subseq_image_path_list[index]
        assert (self.subseq_len_list[index] == len(image_paths))
        seq_len = self.subseq_len_list[index]

        image_sequence = []
        for img_path in image_paths:
            image = self.runtime_transformer(self.image_cache[img_path])
            if self.minus_point_5:
                image = image - 0.5  # from [0, 1] -> [-0.5, 0.5]
            image = self.normalizer(image)
            image_sequence.append(image)
        image_sequence = torch.stack(image_sequence, 0)

        return (seq_len, seq, type, id), image_sequence, gt_rel_poses

    @staticmethod
    def decode_batch_meta_info(batch_meta_info):
        seq_len_list = batch_meta_info[0]
        seq_list = batch_meta_info[1]
        type_list = batch_meta_info[2]
        id_list = batch_meta_info[3]

        return seq_len_list, seq_list, type_list, id_list

    def __len__(self):
        return len(self.data_info.index)
