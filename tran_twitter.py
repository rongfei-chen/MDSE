"""
the code about train
"""
import torch
from torch.utils.tensorboard import SummaryWriter
from torch.optim import Adam, AdamW, SGD
from torch.optim.lr_scheduler import ReduceLROnPlateau
from tqdm import tqdm, trange
import numpy as np
from sklearn.metrics import accuracy_score, f1_score, recall_score, precision_score

from model import ModelParam
from util.write_file import WriteFile
import dev_process


def train_process(opt, train_loader, dev_loader, test_loader, cl_model, loss_func,
                  log_summary_writer: SummaryWriter = None, tokenizer=None, image_id_list=None):
    optimizer = None

    # 调整学习率
    pre_train_model_param = [name for name, param in cl_model.named_parameters() if 'text_model' in name]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in cl_model.named_parameters() if n in pre_train_model_param],
            "lr": 0,
        },
        {
            "params": [p for n, p in cl_model.named_parameters() if n not in pre_train_model_param],
            "lr": opt.lr,
        },
    ]

    if opt.optim == 'adam':
        optimizer = Adam(optimizer_grouped_parameters, betas=(opt.optim_b1, opt.optim_b2))
    elif opt.optim == 'adamw':
        optimizer = AdamW(optimizer_grouped_parameters, betas=(opt.optim_b1, opt.optim_b2))
    elif opt.optim == 'sgd':
        optimizer = SGD(optimizer_grouped_parameters, momentum=opt.momentum)

    scheduler = ReduceLROnPlateau(optimizer, 'min')

    origin_param = ModelParam()
    augment_param = ModelParam()

    last_F1 = 0
    last_Accuracy = 0

    for epoch in trange(opt.epoch, desc='Epoch:'):
        y_true = []
        y_pre = []
        run_loss = 0
        total_labels = 0

        cl_model.train()
        cl_model.zero_grad()

        if epoch == opt.train_fuse_model_epoch:
            optimizer.param_groups[0]['lr'] = opt.lr
            optimizer.param_groups[1]['lr'] = 0

        train_loader_tqdm = tqdm(train_loader, desc='Train Iteration:')
        epoch_step_num = epoch * train_loader_tqdm.total
        step_num = 0
        for index, data in enumerate(train_loader_tqdm):
            texts_origin, bert_attention_mask, image_origin, text_image_mask, labels, \
            texts_augment, bert_attention_mask_augment, image_augment, text_image_mask_augment, target_labels = data

            if opt.cuda is True:
                texts_origin = texts_origin.cuda()
                bert_attention_mask = bert_attention_mask.cuda()
                image_origin = image_origin.cuda()
                text_image_mask = text_image_mask.cuda()
                labels = labels.cuda()
                # texts_augment = texts_augment.cuda()
                # bert_attention_mask_augment = bert_attention_mask_augment.cuda()
                # image_augment = image_augment.cuda()
                # text_image_mask_augment = text_image_mask_augment.cuda()
                for i in range(len(target_labels)):
                    target_labels[i] = target_labels[i].cuda()

            origin_param.set_data_param(texts=texts_origin, bert_attention_mask=bert_attention_mask, images=image_origin,
                                       text_image_mask=text_image_mask)
            # augment_param.set_data_param(texts=texts_augment, bert_attention_mask=bert_attention_mask_augment,
            # images=image_augment, text_image_mask=text_image_mask_augment)

            origin_res, cl_self_loss = cl_model(origin_param, labels, target_labels, kind='train')
            # origin_res = cl_model(origin_param, labels, target_labels, kind='train')

            # 一个是分类任务的误差，另一个则是对比学习的误差
            temp_labels = labels.unsqueeze(-1)
            labels_one_hot = torch.zeros(temp_labels.size(0), 3).cuda()
            labels_one_hot = labels_one_hot.scatter_(1, temp_labels, 1)
            # print(origin_res.shape, labels.shape, l_pos_neg.shape, cl_labels.shape)
            classify_loss = loss_func(origin_res, labels_one_hot)

            # loss = classify_loss
            loss = (classify_loss + cl_self_loss * opt.cl_self_loss_alpha)
            # opt.acc_batch_size
            loss = loss / opt.acc_batch_size
            loss.backward()

            train_loader_tqdm.set_description("Train Iteration, loss: %.6f, lr: %e" %
                                              (loss, optimizer.param_groups[0]['lr']))

            if (index + 1) % opt.acc_grad == 0:
                optimizer.step()
                optimizer.zero_grad()
            step_num += 1

            _, predicted = torch.max(origin_res, 1)
            y_true.extend(labels.cpu())
            y_pre.extend(predicted.cpu())
            run_loss += loss.item()
            total_labels += labels.size(0)

        run_loss /= total_labels
        y_true = np.array(y_true)
        y_pre = np.array(y_pre)
        train_accuracy = accuracy_score(y_true, y_pre)
        train_F1_weighted = f1_score(y_true, y_pre, average='weighted')
        train_R_weighted = recall_score(y_true, y_pre, average='weighted')
        train_precision_weighted = precision_score(y_true, y_pre, average='weighted')
        train_F1 = f1_score(y_true, y_pre, average='macro')
        train_R = recall_score(y_true, y_pre, average='macro')
        train_precision = precision_score(y_true, y_pre, average='macro')

        save_content = 'Epoch: %d:\nTrain: Accuracy: %.6f, F1(weighted): %.6f, ' \
                       'Precision(weighted): %.6f, R(weighted): %.6f, F1(macro): %.6f, ' \
                       'Precision: %.6f, R: %.6f, loss: %.6f' % \
                       (epoch, train_accuracy, train_F1_weighted, train_precision_weighted, train_R_weighted, train_F1,
                        train_precision, train_R, run_loss)
        WriteFile(opt.save_model_path, 'train_correct_log.txt', save_content + '\n', 'a+')
        print(save_content, ' ' * 200)

        if log_summary_writer:
            log_summary_writer.add_scalar('train_info/loss_epoch', run_loss, global_step=epoch)
            log_summary_writer.add_scalar('train_info/acc', train_accuracy, global_step=epoch)
            log_summary_writer.add_scalar('train_info/f1_w', train_F1_weighted, global_step=epoch)
            log_summary_writer.add_scalar('train_info/r_w', train_R_weighted, global_step=epoch)
            log_summary_writer.add_scalar('train_info/p_w', train_precision_weighted, global_step=epoch)
            log_summary_writer.add_scalar('train_info/f1_ma', train_F1, global_step=epoch)
            log_summary_writer.flush()

        train_log = {
            "epoch": epoch,
            "train_accuracy": train_accuracy,
            "train_F1": train_F1,
            "train_R": train_R,
            "train_precision": train_precision,
            "train_F1_weighted": train_F1_weighted,
            "train_precision_weighted": train_precision_weighted,
            "train_R_weighted": train_R_weighted,
            "run_loss": run_loss
        }

        last_F1, last_Accuracy = dev_process.dev_process(opt, loss_func, cl_model, dev_loader, test_loader, last_F1,
                                                         last_Accuracy, train_log, log_summary_writer)


