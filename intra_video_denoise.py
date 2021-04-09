import os.path as osp
import os
import sys

import Levenshtein as ed

import numpy as np

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import (
    cosine_distances,
    euclidean_distances,
    cosine_similarity,
)
from sklearn.cluster import DBSCAN

from collections import defaultdict

from transformers import BertTokenizer, AutoModel
import torch.nn as nn
import torch

from multiprocessing import Process

import jieba.posseg as pseg

from mmcv import ProgressBar


bert_path = "/mnt/lustre/chenghaoyue/projects/mmaction2/work_dirs/bert_model"
# bert_path = "data/bert_model"
tokenizer = None
bert = None
new_root = "/mnt/lustrenew/DATAshare/bilibili/bilibili_intra_denoise"
new_root1 = "data/bilibili/cluster_example"
new_root2 = "data/bilibili/cluster_sample_example"
# new_root = "data/bilibili_intra_denoise"

forbidden_list = ["e", "m", "o", "x", "y", "z"]


############################################# init bert ##################################################


class BERT(nn.Module):
    """BERT backbone.
    """

    def __init__(self, pretrained=None, freeze=True):
        super(BERT, self).__init__()
        self.pretrained = pretrained
        self.freeze = freeze
        self.init_weights()

    def init_weights(self):
        """Initiate the parameters either from existing checkpoint or from
        scratch."""
        if isinstance(self.pretrained, str):
            self.model = AutoModel.from_pretrained(self.pretrained).to("cuda")
            self.model.train()
        else:
            raise TypeError("pretrained must be a str")

    def forward(self, x):
        if self.freeze:
            with torch.no_grad():
                text_out = self.model(**x).pooler_output
        else:
            text_out = self.model(**x).pooler_output
        return text_out


def init_global():
    global tokenizer, bert
    tokenizer = BertTokenizer.from_pretrained(bert_path)
    bert = BERT(bert_path)


############################################# get file directory ##########################################


def get_paths(ROOT, depth=4):
    path_list = set()
    path_list.add(ROOT)
    for i in range(depth):
        tmp_list = set()
        for path in path_list:
            for subdir in os.listdir(path):
                tmp_list.add(osp.join(path, subdir))
        path_list = tmp_list
    return path_list


def read_tree_dir_files_to_file(path, wfile, depth=4):
    """
    read root dir path in depth and write files path into wfile

    :param path:    root directory
    :param wfile:   write the files in the root directory's depth's into wfile
    :param depth:
    :return:
    """
    path_list = get_paths(path, depth)
    with open(wfile, "w", encoding="utf-8") as f:
        f.write("\n".join(path_list))


def save_denoised_file(new_path, time_array, text_list, save_idx, weight):
    os.makedirs(osp.dirname(new_path), exist_ok=True)
    lines = []
    for i, idx in enumerate(save_idx):
        lines.append(
            str(time_array[idx]) + "#*," + text_list[idx] + "#*," + str(weight[i])
        )
    with open(new_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def save_cluster_file(new_path, time_array, text_list, cluster_dict):
    lines = []
    for cluster in cluster_dict:
        idxes = cluster_dict[cluster]
        for idx in idxes:
            lines.append("#*,".join([str(time_array[idx]), text_list[idx]]))
        lines.append("\n")
    with open(new_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


class DataSet:
    def __init__(self, dm_file, feature_file, number):
        with open(dm_file, "r", encoding="utf-8") as f:
            self.dm_paths = [line.strip() for line in f]
        with open(feature_file, "r", encoding="utf-8") as f:
            self.feature_paths = [line.strip() for line in f if "_dm.npz" in line]
        self.path_idx = defaultdict(list)
        self.length = 0
        for i, path in enumerate(self.dm_paths):
            self.path_idx[osp.splitext(osp.basename(path))[0]].append(i)
        for i, path in enumerate(self.feature_paths):
            self.path_idx[osp.basename(path)[: -len("_dm.npz")]].append(i)
        names = list(self.path_idx.keys())
        for name in names:
            if len(self.path_idx[name]) != 2:
                del self.path_idx[name]
        self.keys = sorted(list(self.path_idx.keys())[:number])
        self.length = len(self.keys)
        # self.keys = sorted(list(self.path_idx.keys()))
        # self.length = len(self.path_idx.keys())

    def __len__(self):
        return self.length

    def __getitem__(self, idx):
        idx1, idx2 = self.path_idx[self.keys[idx]]
        return self.dm_paths[idx1], self.feature_paths[idx2]


############################################# read file ###################################################

# read dm file
def read_dm_file(file_name):
    time_list = []
    text_list = []
    with open(file_name, "r", encoding="utf-8") as f:
        try:
            for line in f:
                tokens = line.strip().split("#*,")
                try:
                    time = float(tokens[0])
                    text = tokens[1]
                    time_list.append(time)
                    text_list.append(text)
                except (ValueError, IndexError):
                    pass
        except UnicodeDecodeError:
            print(f"unicode error file:{file_name}")
    return np.array(time_list), text_list


# read feature file
def get_feature(feature_file, text_list):
    data = np.load(feature_file)
    features = data["features"]
    if len(features) != len(text_list):
        number_per_iter = 500
        nums = (len(text_list) + number_per_iter - 1) // number_per_iter
        features = []
        for i in range(nums):
            sub_dm = text_list[i * number_per_iter : (i + 1) * number_per_iter]
            sub_tokens = tokenizer(
                sub_dm, truncation=True, padding="max_length", return_tensors="pt"
            )
            for key in sub_tokens:
                sub_tokens[key] = sub_tokens[key].cuda()
            sub_feat = bert(sub_tokens).cpu().numpy()
            features.append(sub_feat)
        if len(features) > 0:
            features = np.concatenate(features, axis=0)
        else:
            features = np.array(features)
    return features


############################################# filter meaningless text #####################################


def filter_meaningless_text(text_list, time_array, feature_array):
    idxes = []
    filtered_text_list = []
    for i, text in enumerate(text_list):
        words = [
            flag[0] in forbidden_list and flag != "eng" for word, flag in pseg.cut(text)
        ]
        if not all(words):
            idxes.append(i)
            filtered_text_list.append(text)
    idxes = np.array(idxes)
    if len(idxes) == 0:
        return filtered_text_list, np.array([]), np.array([])
    else:
        return filtered_text_list, time_array[idxes], feature_array[idxes]


############################################# compute distance #############################################


def edit_distance(text_list):
    """
    text pairwise edit distance
    :param text_list:    list of text
    :return:
    """
    length = len(text_list)
    distance = np.zeros((length, length))
    for i in range(length):
        texti = text_list[i]
        for j in range(i + 1, length):
            distance[i][j] = ed.distance(texti, text_list[j])
    distance = distance + distance.T
    dmin, dmax = np.min(distance), np.max(distance)
    if dmin != dmax:
        distance = (distance - dmin) / (dmax - dmin)
    elif dmin != 0:
        distance = distance / dmin
    return distance


def tf_idf_distance(text_list):
    """
    :param text_list:
    :param metric:      e: Euclidean distance  c: Cosine distance
    :return:
    """
    # token_list = []
    # for text in text_list:
    #     words = " ".join([word for word, _ in pseg.cut(text)])
    #     token_list.append(words)
    token_list = text_list
    vectorizer = TfidfVectorizer(stop_words=None)
    try:
        tf_idf = vectorizer.fit_transform(token_list)
        distance = cosine_distances(tf_idf)
    except ValueError:
        distance = np.ones((len(text_list), len(text_list)))
    return distance


def tgap_distance(time_array):
    """
    given text time stamp numpy array, return pairwise distance
    :param time_list(float numpy.array):  sorted time stamp list
    :return:
    """
    time_array_copy = time_array - time_array[0]
    distance = abs(time_array_copy.reshape(-1, 1) - time_array_copy.reshape(1, -1))
    tmax = time_array_copy[-1]
    if tmax != 0:
        distance = distance / tmax
    return distance


def feature_distance(feature_array, temperature=0.1):
    """
    given features array, return cosine distances
    :param feature_array(numpy.array):
    :param temperature(float): exp^(dis/temperature)
    :return:
    """
    similarity = cosine_similarity(feature_array)
    similarity = np.exp(similarity / temperature)
    smin, smax = np.min(similarity), np.max(similarity)
    if smin != smax:
        similarity = (similarity - smin) / (smax - smin)
    elif smin != 0:
        similarity = similarity / smin
    distance = 1 - similarity
    return distance


############################################# cluster #########################################################


class IntraFilter:
    def __init__(self, distance_list, distance_weight_list):
        self.disfunc_list = []
        for dis in distance_list:
            if not hasattr(sys.modules[__name__], dis):
                raise ValueError(f"no distance function {dis}!")
            self.disfunc_list.append(getattr(sys.modules[__name__], dis))
        self.distance_weight_list = distance_weight_list
        # self.eps = eps

    def change_weight_list(self, distance_weight_list):
        self.distance_weight_list = distance_weight_list

    def _cluster(
        self, eps, num_samples, text_list, time_array=None, feature_array=None
    ):
        distance_list = []
        for dis in self.disfunc_list:
            if dis.__name__ == "edit_distance" or dis.__name__ == "tf_idf_distance":
                distance_list.append(dis(text_list))
            elif dis.__name__ == "tgap_distance":
                distance_list.append(dis(time_array))
            elif dis.__name__ == "feature_distance":
                distance_list.append(dis(feature_array))
        distance = sum(
            [
                dis * weight
                for dis, weight, in zip(distance_list, self.distance_weight_list)
            ]
        )
        db = DBSCAN(eps=eps, metric="precomputed", min_samples=num_samples).fit(
            distance
        )
        return db

    def cluster(self, eps, num_samples, text_list, time_array=None, feature_array=None):
        db = self._cluster(eps, num_samples, text_list, time_array, feature_array)

        dic = defaultdict(list)
        for i, label in enumerate(db.labels_):
            if label != -1:
                dic[label].append(i)
        centers = []
        center_weight = []
        for cluster in dic.keys():
            centers.append(*np.random.choice(dic[cluster], 1))
            center_weight.append(len(dic[cluster]))
        centers = np.array(centers)
        center_weight = np.array(center_weight)
        idxes = np.argsort(centers)
        centers = centers[idxes]
        center_weight = center_weight[idxes]
        return centers, center_weight

    def get_cluster_info(
        self, eps, num_samples, text_list, time_array=None, feature_array=None
    ):
        db = self._cluster(eps, num_samples, text_list, time_array, feature_array)
        dic = defaultdict(list)
        for i, label in enumerate(db.labels_):
            if label != -1:
                dic[label].append(i)
        centers = []
        center_weight = []
        for cluster in dic.keys():
            centers.append(*np.random.choice(dic[cluster], 1))
            center_weight.append(len(dic[cluster]))
        centers = np.array(centers)
        center_weight = np.array(center_weight)
        idxes = np.argsort(centers)
        centers = centers[idxes]
        center_weight = center_weight[idxes]
        return dic, centers, center_weight


############################################## main ###########################################################


def multi_cluster(dataset, idxes, eps, num_samples):
    pb = ProgressBar(len(idxes))
    pb.start()
    for idx in idxes:
        dm_path, feature_path = dataset[idx]

        base_name = osp.splitext(osp.basename(dm_path))[0] + ".txt"

        # cluster
        # new_name = "/".join(
        #     [*dm_path[dm_path.find("bilibili_dm") :].split("/")[1:-1], base_name]
        # )
        # new_path = osp.join(new_root, new_name)
        # if osp.exists(new_path):
        #     continue

        # get cluster info
        new_name = base_name
        new_path1 = osp.join(new_root1, new_name)
        new_path2 = osp.join(new_root2, new_name)
        os.makedirs(new_root1, exist_ok=True)
        os.makedirs(new_root2, exist_ok=True)

        time_array, text_list = read_dm_file(dm_path)
        feature_array = get_feature(feature_path, text_list)
        text_list, time_array, feature_array = filter_meaningless_text(
            text_list, time_array, feature_array
        )

        if "《 新 二 十 四 孝 图 》 (P1. final_1)" in base_name:
            import pdb

            pdb.set_trace()

        if len(text_list) == 0 or len(time_array) == 0 or len(feature_array) == 0:
            # cluster
            # save_denoised_file(
            #     new_path, np.array([]), np.array([]), np.array([]), np.array([])
            # )

            # cluster info
            pass
        else:
            # cluster
            # centers, center_weight = filter.cluster(
            #     text_list, time_array, feature_array
            # )
            # save_denoised_file(new_path, time_array, text_list, centers, center_weight)

            # cluster_info
            cluster_dict, centers, center_weight = filter.get_cluster_info(
                eps, num_samples, text_list, time_array, feature_array
            )
            save_cluster_file(new_path1, time_array, text_list, cluster_dict)
            save_denoised_file(new_path2, time_array, text_list, centers, center_weight)
        pb.update()


def parse_args():
    import argparse

    parser = argparse.ArgumentParser("")
    parser.add_argument("--weight_list", type=float, nargs="+")
    parser.add_argument("--eps", type=float, default=0.5)
    parser.add_argument("--num_samples", type=int, required=True)
    args = parser.parse_args()
    weight_list = args.weight_list
    eps = args.eps
    num_samples = args.num_samples
    return weight_list, eps, num_samples


if __name__ == "__main__":
    ############################### generate paths file #######################################
    # root1 = "/mnt/lustrenew/DATAshare/bilibili/bilibili_dm"
    # wfile1 = "/mnt/lustre/chenghaoyue/dm_files.txt"
    # # root1 = "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/bilibili_text_feature"
    # # wfile1 = "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/text_feature_files.txt"
    # proc1 = Process(target=read_tree_dir_files_to_file, args=(root1, wfile1))
    # proc1.start()
    # root2 = "/mnt/lustrenew/DATAshare/bilibili/bilibili_text_feature"
    # wfile2 = "/mnt/lustre/chenghaoyue/text_feature_files.txt"
    # # root2 = "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/bilibili_parse_xml"
    # # wfile2 = "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/dm_files.txt"
    # proc2 = Process(target=read_tree_dir_files_to_file, args=(root2, wfile2))
    # proc2.start()
    # proc1.join()
    # proc2.join()

    weight_list, eps, num_samples = parse_args()

    ####################################  load dataset  ######################################
    # feature_files = "/mnt/lustre/chenghaoyue/text_feature_files.txt"
    # text_files = "/mnt/lustre/chenghaoyue/dm_files.txt"
    # feature_files = (
    #     "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/text_feature_files.txt"
    # )
    # text_files = "/home/chenghaoyue/chenghaoyue/code/mmaction2/data/dm_files.txt"
    feature_files = "/mnt/lustre/chenghaoyue/projects/mmaction2/data/bilibili/text_feature_files.txt"
    text_files = "/mnt/lustre/chenghaoyue/projects/mmaction2/data/bilibili/dm_files.txt"
    dataset = DataSet(text_files, feature_files, 100)

    #################################### cluster ##############################################
    distance_list = [
        "edit_distance",
        "tf_idf_distance",
        "tgap_distance",
        "feature_distance",
    ]
    # distance_weight_list = [0.05, 0.05, 0.2, 0.7]
    filter = IntraFilter(distance_list, weight_list)

    proc_num = 10
    procs = []
    data_num_per_proc = (len(dataset) + proc_num - 1) // proc_num
    idxes = list(range(len(dataset)))

    # for i in range(proc_num):
    #     proc = Process(
    #         target=multi_cluster,
    #         args=(
    #             dataset,
    #             idxes[i * data_num_per_proc : (i + 1) * data_num_per_proc],
    #             eps,
    #             num_samples,
    #         ),
    #     )
    #     proc.start()
    #     procs.append(proc)
    # for proc in procs:
    #     proc.join()
    multi_cluster(dataset, idxes, eps, num_samples)
