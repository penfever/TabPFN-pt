import os
import itertools
import argparse
import time
import datetime
import yaml
import json
from contextlib import nullcontext
import copy

import torch
from torch import nn
from torch.utils.data import DataLoader
from torch import autograd
from torch.utils.data import Subset

import tabpfn.utils as utils
from transformer import TransformerModel
from tabpfn.scripts.tabular_evaluation import predict_wrapper
from tabpfn.utils import get_cosine_schedule_with_warmup, get_openai_lr, StoreDictKeyPair, get_weighted_single_eval_pos_sampler, get_uniform_single_eval_pos_sampler
import tabpfn.priors as priors
from tabpfn.priors.real import TabDS
import tabpfn.encoders as encoders
import tabpfn.positional_encodings as positional_encodings
from utils import init_dist, seed_all, EmbeddingConcatenator

from torch.cuda.amp import autocast, GradScaler
from torch import nn

import numpy as np

import uncertainty_metrics.numpy as um

class Losses():
    gaussian = nn.GaussianNLLLoss(full=True, reduction='none')
    mse = nn.MSELoss(reduction='none')
    def ce(num_classes):
        num_classes = num_classes.shape[0] if torch.is_tensor(num_classes) else num_classes
        return nn.CrossEntropyLoss(reduction='none', weight=torch.ones(num_classes))
    bce = nn.BCEWithLogitsLoss(reduction='none')

def train(priordataloader_class, criterion, encoder_generator, emsize=200, nhid=200, nlayers=6, nhead=2, dropout=0.0,
          epochs=10, steps_per_epoch=100, batch_size=200, bptt=10, lr=None, weight_decay=0.0, warmup_epochs=10, input_normalization=False,
          y_encoder_generator=None, pos_encoder_generator=None, decoder=None, extra_prior_kwargs_dict={}, scheduler=get_cosine_schedule_with_warmup,
          load_weights_from_this_state_dict=None, validation_period=10, single_eval_pos_gen=None, bptt_extra_samples=None, gpu_device='cuda:0',
          aggregate_k_gradients=1, verbose=True, style_encoder_generator=None, epoch_callback=None,
          initializer=None, initialize_with_model=None, train_mixed_precision=False, efficient_eval_masking=True, 
          boosting=False, boosting_lr=1e-3, boosting_n_iters=10, rand_init_ensemble=False, do_concat="", **model_extra_args
          ):
    seed_all(extra_prior_kwargs_dict.get('rand_seed'))
    device = gpu_device if torch.cuda.is_available() else 'cpu:0'
    print(f'Using {device} device')
    using_dist, rank, device = init_dist(device)
    if extra_prior_kwargs_dict.get('prior_type') == 'real':
        real_prior = True
    else:
        real_prior = False

    if extra_prior_kwargs_dict.get('prompt_tuning'):
        do_prompt_tuning = True
        prefix_size = extra_prior_kwargs_dict.get('tuned_prompt_size', 100)
    else:
        do_prompt_tuning = False
        prefix_size = 0

    single_eval_pos_gen = single_eval_pos_gen if callable(single_eval_pos_gen) else lambda: single_eval_pos_gen

    def eval_pos_seq_len_sampler():
        single_eval_pos = single_eval_pos_gen()
        if bptt_extra_samples:
            return single_eval_pos, single_eval_pos + bptt_extra_samples
        else:
            return single_eval_pos, bptt
    
    if real_prior:
        X, y = priordataloader_class[0][0], priordataloader_class[0][1]
        num_classes = len(np.unique(y))
        X_val, y_val = priordataloader_class[1][0], priordataloader_class[1][1]
        X_test, y_test = priordataloader_class[2][0], priordataloader_class[2][1]
        #shuffle data
        idx = np.random.permutation(len(X))
        X, y = X[idx], y[idx]
        idx = np.random.permutation(len(X_val))
        X_val, y_val = X_val[idx], y_val[idx]
        idx = np.random.permutation(len(X_test))
        X_test, y_test = X_test[idx], y_test[idx]
        #TODO: make num_features a hyperparameter
        num_features = 100
        train_ds = TabDS(X, y, num_features=num_features, aggregate_k_gradients=aggregate_k_gradients)
        dl = DataLoader(
            train_ds, batch_size=bptt, shuffle=True, num_workers=1, drop_last=True,
        )
        while len(dl) % aggregate_k_gradients != 0:
            bptt += 1
            dl = DataLoader(
                train_ds, batch_size=bptt, shuffle=True, num_workers=1, drop_last=True,
            )
        val_ds = TabDS(X_val, y_val, num_features=num_features, aggregate_k_gradients=1)
        val_dl = DataLoader(
            val_ds, batch_size=bptt, shuffle=False, num_workers=1,
        )
        test_ds = TabDS(X_test, y_test, num_features=num_features, aggregate_k_gradients=1)
        test_dl = DataLoader(
            test_ds, batch_size=bptt, shuffle=False, num_workers=1,
        )
    else:
        dl = priordataloader_class(num_steps=steps_per_epoch, batch_size=batch_size, eval_pos_seq_len_sampler=eval_pos_seq_len_sampler, seq_len_maximum=bptt+(bptt_extra_samples if bptt_extra_samples else 0), device=device, **extra_prior_kwargs_dict)
        num_features = dl.num_features
    encoder = encoder_generator(num_features, emsize)
    #style_def = dl.get_test_batch()[0][0] # the style in batch of the form ((style, x, y), target, single_eval_pos)
    style_def = None
    #print(f'Style definition of first 3 examples: {style_def[:3] if style_def is not None else None}')
    style_encoder = style_encoder_generator(style_def.shape[1], emsize) if (style_def is not None) else None
    if isinstance(criterion, nn.GaussianNLLLoss):
        n_out = 2
    elif isinstance(criterion, nn.CrossEntropyLoss):
        n_out = criterion.weight.shape[0]
    else:
        n_out = 1

    model = TransformerModel(encoder, n_out, emsize, nhead, nhid, nlayers, dropout, style_encoder=style_encoder,
                             y_encoder=y_encoder_generator(1, emsize), input_normalization=input_normalization,
                             pos_encoder=(pos_encoder_generator or positional_encodings.NoPositionalEncoding)(emsize, bptt*2),
                             decoder=decoder, init_method=initializer, efficient_eval_masking=efficient_eval_masking, prefix_size=prefix_size,
                             n_classes=num_classes, **model_extra_args
                             )
    model.criterion = criterion
    if load_weights_from_this_state_dict is not None:
        if load_weights_from_this_state_dict.get('prefix_embedding.weight', None) is None and model.state_dict().get('prefix_embedding.weight', None) is not None:
            load_weights_from_this_state_dict['prefix_embedding.weight'] = model.state_dict()['prefix_embedding.weight']
        model.load_state_dict(load_weights_from_this_state_dict)
    if initialize_with_model is not None:
        model.init_from_small_model(initialize_with_model)

    print(f"Using a Transformer with {sum(p.numel() for p in model.parameters())/1000/1000:.{2}f} M parameters")

    try:
        for (k, v), (k2, v2) in zip(model.state_dict().items(), initialize_with_model.state_dict().items()):
            print(k, ((v - v2) / v).abs().mean(), v.shape)
    except Exception:
        pass

    model.to(device)
    if using_dist:
        print("Distributed training")
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[rank], output_device=rank, broadcast_buffers=False)
    
    if not real_prior:
        dl.model = model

    # learning rate
    if lr is None:
        lr = get_openai_lr(model)
        print(f"Using OpenAI max lr of {lr}.")
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
    scheduler = scheduler(optimizer, warmup_epochs, epochs if epochs is not None else 100) # when training for fixed time lr schedule takes 100 steps

    scaler = GradScaler() if train_mixed_precision else None

    # check that everything uses up-to-date APIs
    utils.check_compatibility(dl)

    eval_model = None

    def tpc_data_eval(cl=1000, X=None, y=None, X_val=None, y_val=None, eval_model=None, val_dl=None):
        from tabpfn import TabPFNClassifier
        print("Evaluating on real data")

        #Pad data with zero features until 100 features are reached
        if X.shape[1] < 100:
            zero_features = np.zeros((X.shape[0], 100 - X.shape[1]))
            X = np.concatenate((X, zero_features), axis=1)
            zero_val_features = np.zeros((X_val.shape[0], 100 - X_val.shape[1]))
            X_val = np.concatenate((X_val, zero_val_features), axis=1)

        eval_model = TabPFNClassifier(device='cuda', 
                                      N_ensemble_configurations=1, 
                                      base_path="/home/benfeuer/TabPFN-pt/tabpfn", 
                                      model_string=extra_prior_kwargs_dict.get('model_string'), 
                                      feature_shift_decoder=False,
                                      no_preprocess_mode=True,
                                      batch_size_inference=1000, 
                                      multiclass_decoder="None",
                                      n_classes=num_classes,
                                      prefix_size=prefix_size,
                                      )
        eval_model.fit(X[:cl, ...], y[:cl, ...], overwrite_warning=True)
        preds = eval_model.predict(X_val)
        correct = np.sum(preds == y_val)
        total = len(y_val)
        tpc_acc = np.round(correct / total, 3)
        print("Loaded model accuracy: ", tpc_acc)
        return tpc_acc
    
    def real_data_eval(r_model, cl=1000, val_dl=None):
        train_data, _, _ = next(iter(dl))
        train_data[0] = train_data[0][:cl, ...]
        train_data[1] = train_data[1][:cl, ...]
        single_eval_pos = len(train_data[0])
        with torch.no_grad():
            correct = 0
            total = len(val_dl.dataset)
            prediction_list = []
            target_list = []
            output_list = []
            for batch, (data, targets, _) in enumerate(val_dl):
                batch_data = tuple([torch.cat((train_data[0], data[0]), dim=0), torch.cat((train_data[1], data[1]), dim=0)])
                output = r_model(tuple(e.to(device) if torch.is_tensor(e) else e for e in batch_data) if isinstance(batch_data, tuple) else batch_data.to(device)
                    , single_eval_pos=single_eval_pos)
                output_list.append(output)
                _, predicted = torch.max(output.cpu().data, 1)
                prediction_list.append(predicted)
                target_list.append(targets)
            outputs = torch.cat(output_list, dim=0)
            predictions = torch.cat(prediction_list, dim=0)
            targets = torch.cat(target_list, dim=0)
            correct += (predictions == targets).sum().item()
        raw_model_acc = np.round(correct / total, 3)
        return raw_model_acc, outputs.cpu(), targets.cpu()
    
    def train_epoch(model, optimizer, boost_this_epoch=False):
        model.train()  # Turn on the train mode
        if do_prompt_tuning:
            model.freeze_parameters_except_prefix()
        total_loss = 0.
        total_positional_losses = 0.
        total_positional_losses_recorded = 0
        nan_steps = 0
        ignore_steps = 0
        before_get_batch = time.time()
        assert len(dl) % aggregate_k_gradients == 0, 'Please set the number of steps per epoch s.t. `aggregate_k_gradients` divides it.'
        # print("Training Dataset size: ", len(dl.dataset))
        for batch, (data, targets, single_eval_pos) in enumerate(dl):
            #print('starting batch', batch, 'of', len(dl))
            if isinstance(data, list):
                data = tuple(data)
            if isinstance(single_eval_pos, torch.Tensor) and single_eval_pos.numel() == 0:
                single_eval_pos = None
            if using_dist and not (batch % aggregate_k_gradients == aggregate_k_gradients - 1):
                cm = model.no_sync()
            else:
                cm = nullcontext()

            if extra_prior_kwargs_dict.get('permute_feature_position_in_ensemble', False):
                data = tuple([data[0][:, torch.randperm(data[0].shape[1])], data[1]])

            with cm:
                time_to_get_batch = time.time() - before_get_batch
                before_forward = time.time()
                if boosting:
                    single_eval_pos = len(targets) // 2
                elif bptt_extra_samples is None:
                    single_eval_pos = single_eval_pos_gen() if callable(single_eval_pos_gen) else single_eval_pos_gen
                else:
                    single_eval_pos = targets.shape[0] - bptt_extra_samples

                with autocast(enabled=scaler is not None):
                    # If style is set to None, it should not be transferred to device
                    output = model(tuple(e.to(device) if torch.is_tensor(e) else e for e in data) if isinstance(data, tuple) else data.to(device)
                                   , single_eval_pos=single_eval_pos)
                    assert output.requires_grad, "Output does not require gradients"
                    forward_time = time.time() - before_forward

                    if single_eval_pos is not None:
                        targets = targets[single_eval_pos:]
                    if isinstance(criterion, nn.GaussianNLLLoss):
                        assert output.shape[-1] == 2, \
                            'need to write a little bit of code to handle multiple regression targets at once'
                        mean_pred = output[..., 0]
                        var_pred = output[..., 1].abs()
                        losses = criterion(mean_pred.flatten(), targets.to(device).flatten(), var=var_pred.flatten())
                    elif isinstance(criterion, (nn.MSELoss, nn.BCEWithLogitsLoss)):
                        losses = criterion(output.flatten(), targets.to(device).flatten())
                    elif isinstance(criterion, nn.CrossEntropyLoss):
                        losses = criterion(output.reshape(-1, n_out), targets.to(device).long().flatten())
                    else:
                        losses = criterion(output, targets)
                    if boosting:
                        loss = losses.mean()
                        nan_share = torch.tensor([0])
                    else:
                        if len(output.shape) == 2:
                            output = output.unsqueeze(1)
                        # print("Losses shape: ", losses.shape)
                        # print("Outputs shape: ", output.shape)
                        losses = losses.view(*output.shape[0:2])

                        loss, nan_share = utils.torch_nanmean(losses.mean(0), return_nanshare=True)
                        loss = loss / aggregate_k_gradients

                if scaler: loss = scaler.scale(loss)
                if boosting and boost_this_epoch:
                    cur_grads = []
                    # Backward pass for each prediction/target pair
                    if prior_grad_dict is None:
                        prior_grad_iter = None
                    else:
                        prior_grad_iter = prior_grad_dict[batch].to(output.device)
                    output_grad = autograd.grad(loss, output)[0]
                    # print("Output grad shape: ", output_grad.shape)
                    gradient_dict[batch] = output_grad.detach().cpu().clone()
                    # cur_grads.append(output_grad.detach().cpu().clone())

                    if prior_grad_iter is not None:
                        grad_shape = output_grad.shape
                        flat_grad = output_grad.flatten()
                        grad_signs = torch.sign(flat_grad)
                        flat_prior_grad = prior_grad_iter.flatten()
                        cur_weight = 0.65
                        flat_grad_new = torch.sqrt(cur_weight * torch.pow(flat_grad, 2) + (1 - cur_weight) * torch.pow(flat_prior_grad, 2))
                        # ones = torch.ones_like(flat_grad)
                        # print("Flat grad shape: ", flat_grad.shape)
                        # print("Flat prior grad shape: ", flat_prior_grad.shape)
                        # flat_grad_new = torch.pow(flat_grad, ones + torch.log(torch.abs(flat_prior_grad)))
                        flat_grad_new_signs = torch.sign(flat_grad_new)
                        flat_grad_new[flat_grad_new_signs != grad_signs] *= -1
                        output_grad = flat_grad_new.reshape(grad_shape)

                    output.backward(output_grad)
                    # gradient_dict[batch] = torch.cat(cur_grads, dim=0)
                else:
                    loss.backward()
                
                if batch % aggregate_k_gradients == aggregate_k_gradients - 1:
                    if scaler: scaler.unscale_(optimizer)
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                    try:
                        if scaler:
                            scaler.step(optimizer)
                            scaler.update()
                        else:
                            optimizer.step()
                    except:
                        print("Invalid optimization step encountered")
                    optimizer.zero_grad()

                step_time = time.time() - before_forward

                if not torch.isnan(loss):
                    total_loss += losses.mean().cpu().detach().item()
                    total_positional_losses += losses.mean(1).cpu().detach() if single_eval_pos is None else \
                        nn.functional.one_hot(torch.tensor(single_eval_pos), bptt)*\
                        losses[:bptt-single_eval_pos].mean().cpu().detach()

                    total_positional_losses_recorded += torch.ones(bptt) if single_eval_pos is None else \
                        nn.functional.one_hot(torch.tensor(single_eval_pos), bptt)
                nan_steps += nan_share
                ignore_steps += (targets == -100).float().mean()
            before_get_batch = time.time()
        #Total positional losses is a torch tensor of size bptt (batch size)
        # print("Total positional losses: ")
        # print(total_positional_losses)
        # print(total_positional_losses.shape)
        # print(total_positional_losses_recorded)
        # print(total_positional_losses_recorded.shape)
        if boosting:
            total_positional_losses = torch.zeros(bptt)
            total_positional_losses_recorded = torch.ones(bptt)
        return total_loss / max(steps_per_epoch, 1), (total_positional_losses / total_positional_losses_recorded).tolist(),\
               time_to_get_batch, forward_time, step_time, nan_steps.cpu().item()/(batch+1),\
               ignore_steps.cpu().item()/(batch+1)

    def concat_embedding(ec, model, method):
        #extract embedding parameters
        device = ec.model.prefix_embedding.weight.device
        if method == "duplicate":
            ec.concatenated_embedding = torch.cat([ec.original_embedding, ec.original_embedding], dim=0).to(device)
            print("concatenated embedding shape: {}".format(ec.concatenated_embedding.shape))
            ec.concatenated_y_embedding = torch.cat([ec.original_y_embedding, ec.original_y_embedding], dim=0).to(device)
            ec.prefix_size = ec.original_prefix_size * 2
        elif method.startswith("rand-init"):
            num_to_concat = min(int(method.split("-")[-1]), len(ec.prefix_weights)+1)                
            print("Concatenating {} embeddings".format(num_to_concat))
            if num_to_concat == 1:
                ec.concatenated_embedding = ec.original_embedding
                ec.concatenated_y_embedding = ec.original_y_embedding
                ec.prefix_size = ec.original_prefix_size
            else:
                ec.concatenated_embedding = torch.cat([ec.original_embedding.to(device)] + [ec.prefix_weights[i]['prefix_weights'].to(device) for i in range(num_to_concat-1)], dim=0).to(device)
                ec.concatenated_y_embedding = torch.cat([ec.original_y_embedding.to(device)] + [ec.prefix_weights[i]['prefix_y_labels'].to(device) for i in range(num_to_concat-1)], dim=0).to(device)
                if "size-ctl" in method:
                    #select random sample of size prefix_size
                    if "perm" in method:
                        sel = torch.randperm(ec.concatenated_embedding.shape[0])[:ec.original_prefix_size].to(device)
                    else:
                        total_emb_size = ec.original_prefix_size
                        emb_size = total_emb_size // num_to_concat
                        orig_emb_size = ec.original_embedding.shape[0]
                        start_pos = [j * orig_emb_size for j in range(num_to_concat)]
                        sel = torch.cat([torch.arange(i, i+emb_size) for i in start_pos], dim=0).to(device)

                    ec.concatenated_embedding = ec.concatenated_embedding[sel]
                    ec.concatenated_y_embedding = ec.concatenated_y_embedding[sel]
                    ec.prefix_size = sel.shape[0]
                else:
                    ec.prefix_size = ec.original_prefix_size * num_to_concat
        else:
            raise NotImplementedError("Method {} not implemented!".format(method))
        model.prefix_embedding.weight = nn.Parameter(ec.concatenated_embedding)
        model.prefix_y_embedding = ec.concatenated_y_embedding
        model.prefix_size = ec.prefix_size
        return model

    def restore_embedding(ec, model):
        model.prefix_embedding.weight = nn.Parameter(ec.original_embedding)
        model.prefix_y_embedding = ec.original_y_embedding
        model.prefix_size = ec.original_prefix_size
        model.freeze_parameters_except_prefix()
        return model
    
    def save_prefix_weights(model, path, i, do_concat, prefix_weights_l):
        # Save prefix weights
        prefix_weights = model.state_dict()['prefix_embedding.weight'].cpu().numpy()
        prefix_fn = f"prefix_weights_{i}.npy"
        prefix_save_path = os.path.join(path, prefix_fn)
        np.save(prefix_save_path, prefix_weights)
        prefix_y_labels = model.prefix_y_embedding.cpu().numpy()
        prefix_y_fn = f"prefix_y_labels_{i}.npy"
        prefix_y_save_path = os.path.join(path, prefix_y_fn)
        np.save(prefix_y_save_path, prefix_y_labels)
        if do_concat:
            prefix_weights_l.append({"prefix_weights": torch.from_numpy(prefix_weights).float(), "prefix_y_labels": torch.from_numpy(prefix_y_labels)})
            print("Prefix weights list length: ", len(prefix_weights_l))
        return prefix_weights_l

    def update_ensemble_acc(boosting_acc):
        ece = np.round(um.ece(labels_np, probs_np, num_bins=30), 3)
        tace = np.round(um.tace(labels_np, probs_np, num_bins=30), 3)
        new_res = {
            "accuracy": boosting_acc,
            "ece": ece,
            "tace": tace,
        }
        return new_res

    def train_test_loop(t_model, t_optim):
        return_outputs = None
        return_targets = None
        res_dict = None
        for epoch in (range(1, epochs + 1) if epochs is not None else itertools.count(1)):
            print('epoch', epoch, 'of', epochs)
            boost_this_epoch = True if epoch == 1 else False
            epoch_start_time = time.time()
            total_loss, total_positional_losses, time_to_get_batch, forward_time, step_time, nan_share, ignore_share =\
                train_epoch(t_model, t_optim, boost_this_epoch)
            val_score = None
            val_score_nc = None
            test_score = None
            if real_prior \
                and (epoch - 1) % validation_period == 0:
                val_score, val_outputs, val_targets = real_data_eval(r_model=t_model, cl=bptt, val_dl=val_dl)
                np_outputs = val_outputs.cpu().numpy().astype(np.float32)
                np_targets = val_targets.cpu().numpy().astype(np.int32)
                val_ece = np.round(um.ece(np_targets, np_outputs, num_bins=30), 3)
                val_tace = np.round(um.tace(np_targets, np_outputs, num_bins=30), 3)
                test_score, test_outputs, test_targets = real_data_eval(r_model=t_model, cl=bptt, val_dl=test_dl)
                np_outputs_t = test_outputs.cpu().numpy().astype(np.float32)
                np_targets_t = test_targets.cpu().numpy().astype(np.int32)
                test_ece = np.round(um.ece(np_targets_t, np_outputs_t, num_bins=30), 3)
                test_tace = np.round(um.tace(np_targets_t, np_outputs_t, num_bins=30), 3)
                return_outputs = val_outputs
                return_targets = val_targets
                if do_prompt_tuning:
                    #TODO: will this work with context length 0? Should this be a hyperparameter?
                    #val_score_nc, outputs = tpc_data_eval(cl=10, X=X, y=y, X_val=X_val, y_val=y_val, eval_model=eval_model, val_dl=val_dl)
                    if do_concat != "":
                        ec = EmbeddingConcatenator(t_model, do_concat, prefix_weights_l)
                        t_model = concat_embedding(ec, t_model, do_concat)
                        val_score_nc, val_outputs, val_targets = real_data_eval(r_model=ec.get_model(), cl=0, val_dl=val_dl)

                        test_score_nc, test_outputs, test_targets = real_data_eval(r_model=ec.get_model(), cl=0, val_dl=test_dl)

                        t_model = restore_embedding(ec, t_model)
                        # Update optimizer parameters to include new embedding
                        t_optim = torch.optim.AdamW(t_model.parameters(), lr=lr, weight_decay=weight_decay)
                    else:
                        val_score_nc, val_outputs, val_targets = real_data_eval(r_model=t_model, cl=0, val_dl=val_dl)
                        test_score_nc, test_outputs, test_targets = real_data_eval(r_model=t_model, cl=0, val_dl=test_dl)
                    np_outputs_nc = val_outputs.cpu().numpy().astype(np.float32)
                    np_targets_nc = val_targets.cpu().numpy().astype(np.int32)
                    val_ece_nc = np.round(um.ece(np_targets_nc, np_outputs_nc, num_bins=30), 3)
                    val_tace_nc = np.round(um.tace(np_targets_nc, np_outputs_nc, num_bins=30), 3)
                    np_outputs_nc_t = test_outputs.cpu().numpy().astype(np.float32)
                    np_targets_nc_t = test_targets.cpu().numpy().astype(np.int32)
                    test_ece_nc = np.round(um.ece(np_targets_nc_t, np_outputs_nc_t, num_bins=30), 3)
                    test_tace_nc = np.round(um.tace(np_targets_nc_t, np_outputs_nc_t, num_bins=30), 3)
            elif hasattr(dl, 'validate') and epoch % validation_period == 0:
                with torch.no_grad():
                    val_score = dl.validate(model)

            if verbose:
                get_time = (time.time() - epoch_start_time)
                print('-' * 89)
                print(
                    f'| end of epoch {epoch:3d} | time: {get_time:5.2f}s | mean loss {total_loss:5.2f} | '
                    #f"| pos losses {','.join([f'{l:5.2f}' for l in total_positional_losses])} | lr {scheduler.get_last_lr()[0]}"
                    f' | data time {time_to_get_batch:5.2f} | step time {step_time:5.2f}'
                    f' | forward time {forward_time:5.2f}' 
                    f' | nan share {nan_share:5.2f} | ignore share (for classification tasks) {ignore_share:5.4f}'
                    + (f' | val score {val_score}' if val_score is not None else '')
                    + (f' | val score nc {val_score_nc}' if val_score_nc is not None else '')
                    + (f' | test score {test_score}' if test_score is not None else '')
                    + (f' | test score nc {test_score_nc}' if test_score_nc is not None else '')
                    + (f' | val ece {val_ece}' if val_score is not None else '')
                    + (f' | val tace {val_tace}' if val_score is not None else '')
                )
                print('-' * 89)
                if val_score is not None:
                    # save the log to a json file
                    res_dict = {'time' : get_time, 
                                'epoch': epoch, 
                                'mean_loss' : total_loss, 
                                'val_score': val_score, 
                                'val_score_nc' : val_score_nc, 
                                "test_score" : test_score, 
                                "test_score_nc" : test_score_nc, 
                                "val_ece" : val_ece, 
                                "val_tace" : val_tace,
                                "test_ece" : test_ece,
                                "test_tace" : test_tace,
                                "val_ece_nc" : val_ece_nc,
                                "val_tace_nc" : val_tace_nc,
                                "test_ece_nc" : test_ece_nc,
                                "test_tace_nc" : test_tace_nc,
                                }
                    mstr = extra_prior_kwargs_dict.get('model_string')
                    boost_iter = f"boost_iter_{cur_boost_iter}" if is_ensemble else ""
                    log_path = os.path.join(extra_prior_kwargs_dict.get('save_path'), f'{mstr}_{boost_iter}_log_{epoch}.json')
                    print("Saving log to json file, path is: ", log_path)
                    with open(log_path, 'w') as f:
                        json.dump(res_dict, f, indent=4)
            
            # todo: res_dict only changes once every 10 epochs?
            if epoch_callback is not None and rank == 0:
                epoch_callback(model, epoch / epochs, res_dict)

            # stepping with wallclock time based scheduler
            scheduler.step()
        return return_outputs, return_targets, res_dict
    
    # main training loop
    bagging = extra_prior_kwargs_dict.get("bagging", False)
    if bagging:
        dl_backup = dl
        split_size = 0.5
        split_indices = []
        for i in range(boosting_n_iters):
            np.random.seed(extra_prior_kwargs_dict.get('rand_seed') + i)
            split_indices.append(np.random.choice(np.arange(len(dl_backup.dataset)), size=int(split_size * len(dl_backup.dataset)), replace=False))
        # dl_backup = dl
        # split_indices = np.array_split(np.arange(len(dl_backup.dataset)), boosting_n_iters)
    is_ensemble = (boosting or bagging or rand_init_ensemble)
    prefix_weights_l = []
    cur_boost_iter = 0
    total_loss = float('inf')
    total_positional_losses = float('inf')
    output_dict = {}
    i = 0
    ensembling_acc = dict()
    try:
        print("Starting training loop \n \n")
        if bagging:
            subset_dataset = Subset(dl_backup.dataset, split_indices[i])
            dl = DataLoader(
                subset_dataset, batch_size=bptt, shuffle=True, num_workers=1, drop_last=True,
            )
        prior_grad_dict = None
        gradient_dict = {}
        output_dict[i], test_targets, results_dict = train_test_loop(model, optimizer)
        prior_grad_dict = gradient_dict
        probs_np = output_dict[0].cpu().numpy()
        labels_np = test_targets.cpu().numpy()
        if is_ensemble:
            ensembling_acc[i] = update_ensemble_acc(results_dict.get('val_score', 0))
        with open(os.path.join(extra_prior_kwargs_dict.get('save_path'), 'ensembling_acc.json'), 'w') as f:
            json.dump(ensembling_acc, f, indent=4)
        if do_prompt_tuning:
            prefix_weights_l = save_prefix_weights(model, extra_prior_kwargs_dict.get('save_path'), i, do_concat, prefix_weights_l)
    except KeyboardInterrupt:
        pass
    
    # boosting logic
    if is_ensemble:
        print("Beginning ensembling")
        for i in range(1, boosting_n_iters):
            if bagging:
                subset_dataset = Subset(dl_backup.dataset, split_indices[i])
                dl = DataLoader(
                    subset_dataset, batch_size=bptt, shuffle=True, num_workers=1, drop_last=True,
                )
            cur_boost_iter = i
            seed_all(extra_prior_kwargs_dict.get('rand_seed') + i)
            print("Ensembling iteration: ", i+1, " of ", boosting_n_iters, "\n \n")
            model.init_prefix_weights()
            output_dict[i], _, results_dict = train_test_loop(model, optimizer)
            prior_grad_dict = gradient_dict
            # Evaluate average model
            if extra_prior_kwargs_dict.get('average_ensemble'):
                current_outs = torch.zeros_like(output_dict[0])
                for j in range(i + 1):
                    current_outs += output_dict[j]
                current_outs /= (i + 1)
            # Evaluate additive model
            else:
                current_outs = output_dict[0]
                for j in range(1, i + 1):
                    boost_res = torch.mul(boosting_lr, output_dict[j])
                    current_outs += boost_res
            _, current_preds = torch.max(current_outs.cpu().data, 1)
            total = len(test_targets)
            correct = (current_preds == test_targets).sum().item()
            boosting_acc = np.round(correct / total, 3)
            probs_np = current_outs.cpu().numpy()
            labels_np = test_targets.cpu().numpy()
            if boosting_acc <= ensembling_acc[i-1]["accuracy"]:
                print("Ensembling accuracy did not improve, ignoring previous iteration")
                output_dict[i] = torch.zeros_like(output_dict[i])
                ensembling_acc[i] = ensembling_acc[i-1]
            else:
                print("Ensembling accuracy is now: ", boosting_acc)
                ensembling_acc[i] = update_ensemble_acc(boosting_acc)
            if do_prompt_tuning:
                prefix_weights_l = save_prefix_weights(model, extra_prior_kwargs_dict.get('save_path'), i, do_concat, prefix_weights_l)
        # Save ensembling accuracy
        with open(os.path.join(extra_prior_kwargs_dict.get('save_path'), 'ensembling_acc.json'), 'w') as f:
            json.dump(ensembling_acc, f, indent=4)

    # break down training and return
    if rank == 0: # trivially true for non-parallel training
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model = model.module
            dl = None
        return total_loss, total_positional_losses, model.to('cpu'), dl

def _parse_args(config_parser, parser):
    # Do we have a config file to parse?
    args_config, remaining = config_parser.parse_known_args()
    if args_config.config:
        with open(args_config.config, 'r') as f:
            cfg = yaml.safe_load(f)
            parser.set_defaults(**cfg)

    # The main arg parser parses the rest of the args, the usual
    # defaults will have been overridden if config file specified.
    args = parser.parse_args(remaining)

    # Cache the args as a text string to save them in the output dir later
    args_text = yaml.safe_dump(args.__dict__, default_flow_style=False)
    return args, args_text


if __name__ == '__main__':
    config_parser = argparse.ArgumentParser(description='Only used as a first parser for the config file path.')
    config_parser.add_argument('--config')
    parser = argparse.ArgumentParser()
    parser.add_argument('prior')
    parser.add_argument('--loss_function', default='gaussnll')
    # Optional Arg's for `--loss_function barnll`
    parser.add_argument('--min_y', type=float, help='barnll can only model y in strict ranges, this is the minimum y can take.')
    parser.add_argument('--max_y', type=float, help='barnll can only model y in strict ranges, this is the maximum y can take.')
    parser.add_argument('--num_features', default=None, type=int, help='Specify depending on the prior (can be None).')
    #parser.add_argument('--num_features', default=None, type=int, help='Specify depending on the prior.')
    parser.add_argument("--extra_prior_kwargs_dict", default={}, dest="extra_prior_kwargs_dict", action=StoreDictKeyPair, nargs="+", metavar="KEY=VAL", help='Specify depending on the prior.')
    parser.add_argument('--encoder', default='linear', type=str, help='Specify depending on the prior.')
    parser.add_argument('--y_encoder', default='linear', type=str, help='Specify depending on the prior. You should specify this if you do not fuse x and y.')
    parser.add_argument('--pos_encoder', default='none', type=str, help='Specify depending on the prior.')
    parser.add_argument('--bptt', default=10, type=int)
    parser.add_argument('--epochs', default=200, type=int)
    parser.add_argument('--warmup_epochs', default=50, type=int)
    parser.add_argument('--validation_period', default=10, type=int)
    parser.add_argument('--permutation_invariant_max_eval_pos', default=None, type=int, help='Set this to an int to ')
    parser.add_argument('--permutation_invariant_sampling', default='weighted', help="Only relevant if --permutation_invariant_max_eval_pos is set.")
    parser.add_argument('--train_mixed_precision', action='store_true')

    # these can likely be mostly left at defaults
    parser.add_argument('--emsize', default=512, type=int) # sometimes even larger is better e.g. 1024
    parser.add_argument('--nlayers', default=6, type=int)
    parser.add_argument('--nhid', default=None, type=int) # 2*emsize is the default
    parser.add_argument('--nhead', default=4, type=int) # nhead = emsize / 64 in the original paper
    parser.add_argument('--dropout', default=.0, type=float)
    parser.add_argument('--steps_per_epoch', default=10, type=int)
    parser.add_argument('--batch_size', default=1000, type=int)
    parser.add_argument('--lr', '--learning_rate', default=.001, type=float) # try also .0003, .0001, go lower with lower batch size

    args, _ = _parse_args(config_parser, parser)

    if args.nhid is None:
        args.nhid = 2*args.emsize

    prior = args.__dict__.pop('prior')

    if prior == 'gp':
        prior = priors.fast_gp.DataLoader
    elif prior == 'ridge':
        prior = priors.ridge.DataLoader
    elif prior == 'stroke':
        prior = priors.stroke.DataLoader
    elif prior == 'mix_gp':
        prior = priors.fast_gp_mix.DataLoader
    else:
        raise NotImplementedError(f'Prior == {prior}.')

    loss_function = args.__dict__.pop('loss_function')

    criterion = nn.GaussianNLLLoss(reduction='none', full=True)
    classificiation_criterion = nn.CrossEntropyLoss(reduction='none')
    max_y = args.__dict__.pop('max_y')
    min_y = args.__dict__.pop('min_y')
    # criterion = nn.MSELoss(reduction='none')

    if args.num_features:
        extra_prior_kwargs_dict["num_features"] = args.num_features

    if loss_function == 'ce':
        criterion = nn.CrossEntropyLoss(reduction='none')
    elif loss_function == 'gaussnll':
        criterion = nn.GaussianNLLLoss(reduction='none', full=True)
    elif loss_function == 'mse':
        criterion = nn.MSELoss(reduction='none')
    else:
        raise NotImplementedError(f'loss_function == {loss_function}.')



    encoder = args.__dict__.pop('encoder')
    y_encoder = args.__dict__.pop('y_encoder')

    def get_encoder_generator(encoder):
        if encoder == 'linear':
            encoder_generator = encoders.Linear
        elif encoder == 'mlp':
            encoder_generator = encoders.MLP
        elif encoder == 'positional':
            encoder_generator = encoders.Positional
        else:
            raise NotImplementedError(f'A {encoder} encoder is not valid.')
        return encoder_generator

    encoder_generator = get_encoder_generator(encoder)
    y_encoder_generator = get_encoder_generator(y_encoder)

    pos_encoder = args.__dict__.pop('pos_encoder')

    if pos_encoder == 'none':
        pos_encoder_generator = None
    elif pos_encoder == 'sinus':
        pos_encoder_generator = positional_encodings.PositionalEncoding
    elif pos_encoder == 'learned':
        pos_encoder_generator = positional_encodings.LearnedPositionalEncoding
    elif pos_encoder == 'paired_scrambled_learned':
        pos_encoder_generator = positional_encodings.PairedScrambledPositionalEncodings
    else:
        raise NotImplementedError(f'pos_encoer == {pos_encoder} is not valid.')

    permutation_invariant_max_eval_pos = args.__dict__.pop('permutation_invariant_max_eval_pos')
    permutation_invariant_sampling = args.__dict__.pop('permutation_invariant_sampling')
    if permutation_invariant_max_eval_pos is not None:
        if permutation_invariant_sampling == 'weighted':
            get_sampler = get_weighted_single_eval_pos_sampler
        elif permutation_invariant_sampling == 'uniform':
            get_sampler = get_uniform_single_eval_pos_sampler
        else:
            raise ValueError()
        args.__dict__['single_eval_pos_gen'] = get_sampler(permutation_invariant_max_eval_pos)


    print("ARGS for `train`:", args.__dict__)

    train(prior, criterion, encoder_generator,
          y_encoder_generator=y_encoder_generator, pos_encoder_generator=pos_encoder_generator,
          **args.__dict__)
