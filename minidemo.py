"""Train the order embedding model"""

# Set this flag to True to use hyperparameter optimization
# We use Testtube for hyperparameter tuning
HYPERPARAM_SEARCH = False
HYPERPARAM_SEARCH_N_TRIALS = None  # how many grid search trials to run
#    (set to None for exhaustive search)

import argparse
from itertools import permutations
import pickle
from queue import PriorityQueue
import os
import random
import time

from deepsnap.batch import Batch
import networkx as nx
import numpy as np
from sklearn.manifold import TSNE
import torch
import torch.nn as nn
import torch.multiprocessing as mp
import torch.nn.functional as F
import torch.optim as optim
# from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter
from torch_geometric.data import DataLoader
from torch_geometric.datasets import TUDataset
import torch_geometric.utils as pyg_utils
import torch_geometric.nn as pyg_nn
from common.data import *
from common import data
from common import models
from common import utils

if HYPERPARAM_SEARCH:
    from test_tube import HyperOptArgumentParser
    from subgraph_matching.hyp_search import parse_encoder
else:
    from subgraph_matching.config import parse_encoder
from subgraph_matching.test import validation


def build_model(args):
    # build model
    if args.method_type == "order":
        model = models.OrderEmbedder(1, args.hidden_dim, args)
    elif args.method_type == "mlp":
        model = models.BaselineMLP(1, args.hidden_dim, args)
    model.to(utils.get_device())
    if args.test and args.model_path:
        model.load_state_dict(torch.load(args.model_path,
                                         map_location=utils.get_device()))
    return model


def make_data_source(args):
    toks = args.dataset.split("-")
    if toks[0] == "syn":
        if len(toks) == 1 or toks[1] == "balanced":
            data_source = data.OTFSynDataSource(
                node_anchored=args.node_anchored)
        elif toks[1] == "imbalanced":
            data_source = data.OTFSynImbalancedDataSource(
                node_anchored=args.node_anchored)
        else:
            raise Exception("Error: unrecognized dataset")
    else:
        if len(toks) == 1 or toks[1] == "balanced":
            data_source = data.DiskDataSource(toks[0],
                                              node_anchored=args.node_anchored)
        elif toks[1] == "imbalanced":
            data_source = data.DiskImbalancedDataSource(toks[0],
                                                        node_anchored=args.node_anchored)
        else:
            raise Exception("Error: unrecognized dataset")
    return data_source


def train(args, model, logger, in_queue, out_queue):
    """Train the order embedding model.

    args: Commandline arguments
    logger: logger for logging progress
    in_queue: input queue to an intersection computation worker
    out_queue: output queue to an intersection computation worker
    """
    scheduler, opt = utils.build_optimizer(args, model.parameters())
    # 配置嵌入网络模型的优化器
    if args.method_type == "order":
        # 配置
        clf_opt = optim.Adam(model.clf_model.parameters(), lr=args.lr)

    done = False
    while not done:
        data_source = make_data_source(args)
        loaders = data_source.gen_data_loaders(args.eval_interval * args.batch_size, args.batch_size, train=True)
        count = 0
        # for batch_target, batch_neg_target, batch_neg_query in zip(*loaders):
        #     print("*" * 20)
        #     print("batch_target:", batch_target)
        #     print("batch_neg_target:", batch_neg_target)
        #     print("batch_neg_query:", batch_neg_query)

        # zip
        a = [1, 2, 3]

        for batch_target, batch_neg_target, batch_neg_query in zip(*loaders):
            # print("begin get queue!")
            # msg, _ = in_queue.get()
            # if msg == "done":
            #     done = True
            #     break
            # train
            model.train()
            model.zero_grad()
            print("begin generate data!")
            pos_a, pos_b, neg_a, neg_b = data_source.gen_batch(batch_target, batch_neg_target, batch_neg_query, True)
            # emb_pos_a= model.emb_model(pos_a)
            emb_pos_a, emb_pos_b = model.emb_model(pos_a), model.emb_model(pos_b)
            emb_neg_a, emb_neg_b = model.emb_model(neg_a), model.emb_model(neg_b)
            # print(emb_pos_a.shape, emb_neg_a.shape, emb_neg_b.shape)
            emb_as = torch.cat((emb_pos_a, emb_neg_a), dim=0)
            emb_bs = torch.cat((emb_pos_b, emb_neg_b), dim=0)
            labels = torch.tensor([1] * pos_a.num_graphs + [0] * neg_a.num_graphs).to(
                utils.get_device())
            intersect_embs = None
            pred = model(emb_as, emb_bs)
            loss = model.criterion(pred, intersect_embs, labels)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            if scheduler:
                scheduler.step()

            if args.method_type == "order":
                with torch.no_grad():
                    pred = model.predict(pred)
                model.clf_model.zero_grad()
                pred = model.clf_model(pred.unsqueeze(1))
                criterion = nn.NLLLoss()
                clf_loss = criterion(pred, labels)
                clf_loss.backward()
                clf_opt.step()
            pred = pred.argmax(dim=-1)
            acc = torch.mean((pred == labels).type(torch.float))
            train_loss = loss.item()
            train_acc = acc.item()

            out_queue.put(("step", (loss.item(), acc)))


def train_loop(args):
    if not os.path.exists(os.path.dirname(args.model_path)):
        os.makedirs(os.path.dirname(args.model_path))
    if not os.path.exists("plots/"):
        os.makedirs("plots/")

    print("Starting {} workers".format(args.n_workers))
    in_queue, out_queue = mp.Queue(), mp.Queue()

    print("Using dataset {}".format(args.dataset))

    record_keys = ["conv_type", "n_layers", "hidden_dim",
                   "margin", "dataset", "max_graph_size", "skip"]
    args_str = ".".join(["{}={}".format(k, v)
                         for k, v in sorted(vars(args).items()) if k in record_keys])
    logger = SummaryWriter(comment=args_str)

    model = build_model(args)
    model.share_memory()

    # if args.method_type == "order":
    #     clf_opt = optim.Adam(model.clf_model.parameters(), lr=args.lr)
    # else:
    #     clf_opt = None

    data_source = make_data_source(args)
    loaders = data_source.gen_data_loaders(args.val_size, args.batch_size,
                                           train=False, use_distributed_sampling=False)
    test_pts = []
    for batch_target, batch_neg_target, batch_neg_query in zip(*loaders):
        pos_a, pos_b, neg_a, neg_b = data_source.gen_batch(batch_target,
                                                           batch_neg_target, batch_neg_query, False)
        if pos_a:
            pos_a = pos_a.to(torch.device("cpu"))
            pos_b = pos_b.to(torch.device("cpu"))
        neg_a = neg_a.to(torch.device("cpu"))
        neg_b = neg_b.to(torch.device("cpu"))
        test_pts.append((pos_a, pos_b, neg_a, neg_b))

    workers = []
    for i in range(args.n_workers):
        worker = mp.Process(target=train, args=(args, model, data_source,
                                                in_queue, out_queue))
        worker.start()
        workers.append(worker)
    # train(args, model, data_source, in_queue, out_queue)
    args.test = False
    if args.test:
        validation(args, model, test_pts, logger, 0, 0, verbose=True)
    else:
        batch_n = 0
        for epoch in range(args.n_batches // args.eval_interval):
            for i in range(args.eval_interval):
                in_queue.put(("step", None))
            for i in range(args.eval_interval):
                msg, params = out_queue.get()
                train_loss, train_acc = params
                print("Batch {}. Loss: {:.4f}. Training acc: {:.4f}".format(
                    batch_n, train_loss, train_acc), end="               \r")
                logger.add_scalar("Loss/train", train_loss, batch_n)
                logger.add_scalar("Accuracy/train", train_acc, batch_n)
                batch_n += 1
            validation(args, model, test_pts, logger, batch_n, epoch)

    for i in range(args.n_workers):
        in_queue.put(("done", None))
    for worker in workers:
        worker.join()


parser = (argparse.ArgumentParser(description='Order embedding arguments')
          if not HYPERPARAM_SEARCH else
          HyperOptArgumentParser(strategy='grid_search'))

utils.parse_optimizer(parser)
parse_encoder(parser)
args = parser.parse_args()

model = build_model(args)
model.share_memory()

data_source = make_data_source(args)
# loaders = data_source.gen_data_loaders(args.val_size, args.batch_size, train=False, use_distributed_sampling=False)
in_queue, out_queue = mp.Queue(), mp.Queue()
train(args, model, data_source, in_queue, out_queue)
