# HAT: Hardware-Aware Transformers for Efficient Natural Language Processing
# Hanrui Wang, Zhanghao Wu, Zhijian Liu, Han Cai, Ligeng Zhu, Chuang Gan and Song Han
# The 58th Annual Meeting of the Association for Computational Linguistics (ACL), 2020.
# Paper: https://arxiv.org/abs/2005.14187
# Project page: https://hanruiwang.me/project_pages/hat/

import collections

import math
import random
import torch
import pdb

from scipy.stats import kendalltau, spearmanr

from fairseq import checkpoint_utils, distributed_utils, options, progress_bar, tasks, utils
from fairseq.data import iterators
from fairseq.trainer import Trainer
from fairseq.meters import AverageMeter, StopwatchMeter
from copy import deepcopy


def main(args, init_distributed=False):
    utils.import_user_module(args)
    utils.handle_save_path(args)

    assert args.max_tokens is not None or args.max_sentences is not None, \
        'Must specify batch size either with --max-tokens or --max-sentences'

    # Initialize CUDA and distributed training
    if torch.cuda.is_available() and not args.cpu:
        torch.cuda.set_device(args.device_id)
    torch.manual_seed(args.seed)
    if init_distributed:
        args.distributed_rank = distributed_utils.distributed_init(args)

    if distributed_utils.is_master(args):
        checkpoint_utils.verify_checkpoint_directory(args.save_dir)

    # Print args
    print(f"| Configs: {args}")

    # Setup task, e.g., translation, language modeling, etc.
    task = tasks.setup_task(args)

    # Load valid dataset (we load training data below, based on the latest checkpoint)
    for valid_sub_split in args.valid_subset.split(','):
        task.load_dataset(valid_sub_split, combine=False, epoch=0)
    if args.task == 'classification':
        args.nb_classes = task.datasets[valid_sub_split].nb_classes
    # Build model and criterion
    model = task.build_model(args)
    criterion = task.build_criterion(args)
    print(
        f"| Model: {args.arch} \n| Criterion: {criterion.__class__.__name__}")

    # Log architecture
    if args.train_subtransformer:
        print(" \n\n\t\tWARNING!!! Training one single SubTransformer\n\n")
        print(
            f"| SubTransformer Arch: {utils.get_subtransformer_config(args)} \n")
    else:
        print(" \n\n\t\tWARNING!!! Training SuperTransformer\n\n")
        print(f"| SuperTransformer Arch: {model} \n")

    # Log model size
    if args.train_subtransformer:
        model_size = model.get_sampled_params_numel(utils.get_subtransformer_config(args))
        if args.task == 'translation':
            embed_size = args.decoder_embed_dim_subtransformer * len(task.tgt_dict)
            print(f"| SubTransformer size (without embedding weights): {model_size} \n")
            print(f"| Embedding layer size: {embed_size} \n")
        elif args.task == 'language_modeling':
            print(f"| Total parameters: {model_size} \n")

    else:
        model_s = 0
        # if use model.state_dict, then will add 2 more parameters, they are encoder.version and decoder.version. Should not count them
        for name, param in model.named_parameters():
            if 'embed' not in name:
                model_s += param.numel()
        print(
            f"| SuperTransofmer model size (without embedding weights): {model_s}")

        print(
            f"| Embedding layer size: {sum(p.numel() for p in model.parameters() if p.requires_grad) - model_s} \n")

    # specify the length of the dummy input for profile
    # for iwslt, the average length is 23, for wmt, that is 30
    if args.task == 'translation':
        dummy_sentence_length_dict = {'iwslt': 23, 'wmt': 30}
        if 'iwslt' in args.arch:
            dummy_sentence_length = dummy_sentence_length_dict['iwslt']
        elif 'wmt' in args.arch:
            dummy_sentence_length = dummy_sentence_length_dict['wmt']
        else:
            raise NotImplementedError
    elif args.task == 'language_modeling':
        dummy_sentence_length = args.max_tokens
    if args.task != 'classification':
        dummy_src_tokens = [2] + [7] * (dummy_sentence_length - 1)
        dummy_prev = [7] * (dummy_sentence_length - 1) + [2]
    elif args.task == 'classification':
        dummy_src_tokens = torch.randn(1, 3, 224, 224)
        dummy_prev = None
    # profile the overall FLOPs number
    if args.profile_flops:
        import torchprofile
        config_subtransformer = utils.get_subtransformer_config(args)
        model.set_sample_config(config_subtransformer)
        model.profile(mode=True)
        if args.task == 'classification':
            macs = torchprofile.profile_macs(model, args=(dummy_src_tokens))
            model.profile(mode=False)
            last_layer_macs = 0
        else:
            macs = torchprofile.profile_macs(model, args=(
                torch.tensor([dummy_src_tokens],
                             dtype=torch.long), torch.tensor([30]),
                torch.tensor([dummy_prev], dtype=torch.long)))
            model.profile(mode=False)

            last_layer_macs = config_subtransformer['decoder']['decoder_embed_dim'] * dummy_sentence_length * len(
                task.tgt_dict)

        print(f"| Total FLOPs: {macs * 2}")
        print(f"| Last layer FLOPs: {last_layer_macs * 2}")
        print(
            f"| Total FLOPs without last layer: {(macs - last_layer_macs) * 2} \n")
        exit(0)

    # Build trainer
    trainer = Trainer(args, task, model, criterion)
    print(f"| Training on {args.distributed_world_size} GPUs")
    print(
        f"| Max tokens per GPU = {args.max_tokens} and max sentences per GPU = {args.max_sentences} \n")
    # Measure model latency, the program will exit after profiling latency
    if args.latcpu or args.latgpu:
        utils.measure_latency(args, model, dummy_src_tokens, dummy_prev)
        exit(0)

    # Load the latest checkpoint if one is available and restore the corresponding train iterator
    extra_state, epoch_itr = checkpoint_utils.load_checkpoint(args, trainer)

    # Evaluate the SubTransformer
    if args.validate_subtransformer:
        config = utils.get_subtransformer_config(args)
        trainer.set_sample_config(config)
        valid_loss = validate(args, trainer, task, epoch_itr, [
                              'valid'], 'SubTransformer')
        print(f"| SubTransformer validation loss:{valid_loss}")

    # Loop boundaries
    max_epoch = args.max_epoch or math.inf
    max_update = args.max_update or math.inf
    lr = trainer.get_lr()

    train_meter = StopwatchMeter()
    train_meter.start()
    valid_subsets = args.valid_subset.split(',')

    represent_configs = utils.get_represent_configs(args)

    sampled_archs = []
    hash_tables = []

    while len(sampled_archs) < args.rank_list_size:
        sample = utils.sample_configs(
            utils.get_all_choices(args),
            reset_rand_seed=False,
            super_decoder_num_layer=args.decoder_layers
        )
        if str(sample) not in hash_tables:
            sampled_archs.append(sample)
            hash_tables.append(str(sample))
    prev_loss = [i * 0.01 for i in range(args.rank_list_size)]
    corr = -1.0

    def get_rank_corr(prev_corr_, prev_loss_):
        losses = []
        for i, arch in enumerate(sampled_archs):
            trainer.set_sample_config(config=arch)
            valid_losses = validate(
                args, trainer, task, epoch_itr, valid_subsets, str(i))
            losses.append(valid_losses[0])
            del valid_losses
        corr = kendalltau(losses, prev_loss_)[0]
        spearman_corr = spearmanr(losses, prev_loss_)[0]
        is_ranking_stable = corr > prev_corr_

        print('current spearman corr:', spearman_corr)
        print('current kendalltau corr:', corr)

        return is_ranking_stable, corr, losses
    
    ranking_counter = 0
    stop_training = False

    # Main training loop
    while not stop_training and lr > args.min_lr and epoch_itr.epoch < max_epoch and trainer.get_num_updates() < max_update:

        # train for one epoch
        train(args, trainer, task, epoch_itr)

        # apply early stopping if the ranking corr doesn't change for multiple rounds
        if args.task != 'classification':
            is_ranking_stable, corr, prev_loss = get_rank_corr(corr, prev_loss)
        else:
            is_ranking_stable = False
            corr = 0
        if is_ranking_stable:
            ranking_counter += 1
        else:
            ranking_counter = 0

        if ranking_counter > args.ranking_patience or corr > args.corr_threshold:
            print('| The ranking is stable, stop training')
            stop_training = True

        if not args.disable_validation and epoch_itr.epoch % args.validate_interval == 0:
            for k, v in represent_configs.items():
                trainer.set_sample_config(config=v)
                valid_losses = validate(
                    args, trainer, task, epoch_itr, valid_subsets, sampled_arch_name=k)
        else:
            valid_losses = [None]
        
        # save checkpoint epoch level
        if epoch_itr.epoch % args.save_interval == 0:
            checkpoint_utils.save_checkpoint(
                args, trainer, epoch_itr, valid_losses[0])

        # update the best loss and get current lr; the real lr scheduling is done in trainer.train_step()
        lr = trainer.lr_step(epoch_itr.epoch, valid_losses[0])

    train_meter.stop()
    print('| Done training in {:.1f} seconds'.format(train_meter.sum))


def train(args, trainer, task, epoch_itr):
    """Train the model for one epoch."""
    # Update parameters every N batches
    update_freq = args.update_freq[epoch_itr.epoch - 1] \
        if epoch_itr.epoch <= len(args.update_freq) else args.update_freq[-1]

    # Initialize data iterator
    itr = epoch_itr.next_epoch_itr(
        fix_batches_to_gpus=args.fix_batches_to_gpus,
        shuffle=(epoch_itr.epoch >= args.curriculum),
    )
    itr = iterators.GroupedIterator(itr, update_freq)
    progress = progress_bar.build_progress_bar(
        args, itr, epoch_itr.epoch,
    )

    extra_meters = collections.defaultdict(lambda: AverageMeter())
    valid_subsets = args.valid_subset.split(',')

    max_update = args.max_update or math.inf

    represent_configs = utils.get_represent_configs(args)
    for i, samples in enumerate(progress, start=epoch_itr.iterations_in_epoch):

        if args.train_subtransformer:
            # training one SubTransformer only
            configs = [utils.get_subtransformer_config(args)]
        else:
            # training SuperTransformer by randomly sampling SubTransformers
            if args.task == 'translation':
                configs = [utils.sample_configs(utils.get_all_choices(args), reset_rand_seed=True,
                                                rand_seed=trainer.get_num_updates(),
                                                super_decoder_num_layer=args.decoder_layers)]
            elif args.task == 'language_modeling':
                configs = [utils.sample_configs_lm(utils.get_all_choices(args), reset_rand_seed=True,
                                                   rand_seed=trainer.get_num_updates(),
                                                   super_decoder_num_layer=args.decoder_layers)]
            elif args.task == 'classification':
                configs = [utils.sample_configs(utils.get_all_choices(args), reset_rand_seed=True,
                                                rand_seed=trainer.get_num_updates(),
                                                super_decoder_num_layer=args.decoder_layers)]
        log_output = trainer.train_step(samples, configs=configs)
        if log_output is None:
            continue

        # log mid-epoch stats
        stats = utils.get_training_stats(trainer)
        for k, v in log_output.items():
            if k in ['loss', 'nll_loss', 'ntokens', 'nsentences', 'sample_size']:
                continue  # these are already logged above
            if 'loss' in k or k == 'accuracy':
                extra_meters[k].update(v, log_output['sample_size'])
            else:
                extra_meters[k].update(v)
            stats[k] = extra_meters[k].avg

        utils.log_arch_info(stats, configs[0])

        progress.log(stats, tag='train', step=stats['num_updates'])

        # ignore the first mini-batch in words-per-second calculation
        if i == 0:
            trainer.get_meter('wps').reset()

        num_updates = trainer.get_num_updates()
        if (
                not args.disable_validation
                and args.save_interval_updates > 0
                and num_updates % args.save_interval_updates == 0
                and num_updates > 0
        ):
            for k, v in represent_configs.items():
                trainer.set_sample_config(config=v)
                valid_losses = validate(
                    args, trainer, task, epoch_itr, valid_subsets, sampled_arch_name=k)

            checkpoint_utils.save_checkpoint(
                args, trainer, epoch_itr, valid_losses[0])
        if num_updates >= max_update or i == (args.max_batch - 1):
            break

    # log end-of-epoch stats
    stats = utils.get_training_stats(trainer)
    for k, meter in extra_meters.items():
        stats[k] = meter.avg
    progress.print(stats, tag='train', step=stats['num_updates'])

    # reset training meters
    for k in [
        'train_loss', 'train_nll_loss', 'wps', 'ups', 'wpb', 'bsz', 'gnorm', 'clip',
    ]:
        meter = trainer.get_meter(k)
        if meter is not None:
            meter.reset()


def validate(args, trainer, task, epoch_itr, subsets, sampled_arch_name):
    """Evaluate the model on the validation set(s) and return the losses."""
    valid_losses = []
    for subset in subsets:
        # Initialize data iterator
        def get_itr():
            itr = task.get_batch_iterator(
                dataset=task.dataset(subset),
                max_tokens=args.max_tokens_valid,
                max_sentences=args.max_sentences_valid,
                max_positions=utils.resolve_max_positions(
                    task.max_positions(),
                    trainer.get_model().max_positions(),
                ),
                ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
                required_batch_size_multiple=args.required_batch_size_multiple,
                seed=args.seed,
                num_shards=args.distributed_world_size,
                shard_id=args.distributed_rank,
                num_workers=args.num_workers,
            ).next_epoch_itr(shuffle=False)
            progress = progress_bar.build_progress_bar(
                args, itr, epoch_itr.epoch,
                prefix='valid on \'{}\' subset'.format(subset),
                no_progress_bar='simple'
            )
            return progress

        progress = get_itr()

        # reset validation loss meters
        for k in ['valid_loss', 'valid_nll_loss']:
            meter = trainer.get_meter(k)
            if meter is not None:
                meter.reset()
        extra_meters = collections.defaultdict(lambda: AverageMeter())

        for sample in progress:
            log_output = trainer.valid_step(sample)

            for k, v in log_output.items():
                if k in ['loss', 'nll_loss', 'ntokens', 'nsentences', 'sample_size']:
                    continue
                extra_meters[k].update(v)

        # log validation stats
        stats = utils.get_valid_stats(trainer, args)
        for k, meter in extra_meters.items():
            stats[k] = meter.avg

        # log validation stats
        stats = utils.get_valid_stats(trainer, args, extra_meters)

        stats[sampled_arch_name + '_loss'] = deepcopy(stats['loss'])
        stats[sampled_arch_name + '_nll_loss'] = deepcopy(stats['nll_loss'])
        stats[sampled_arch_name + '_acc1'] = deepcopy(stats['valid_acc1'])
        stats[sampled_arch_name + '_acc5'] = deepcopy(stats['valid_acc5'])
        for k, meter in extra_meters.items():
            stats[k] = meter.avg

        progress.print(stats, tag=subset, step=trainer.get_num_updates())

        valid_losses.append(
            stats[args.best_checkpoint_metric].avg
            if args.best_checkpoint_metric == 'loss'
            else stats[args.best_checkpoint_metric]
        )
    return valid_losses


def distributed_main(i, args, start_rank=0):
    args.device_id = i
    if args.distributed_rank is None:  # torch.multiprocessing.spawn
        args.distributed_rank = start_rank + i
    main(args, init_distributed=True)


def cli_main():
    parser = options.get_training_parser()

    parser.add_argument('--train-subtransformer', action='store_true', default=False,
                        help='whether train SuperTransformer or SubTransformer')
    parser.add_argument('--sub-configs', required=False, is_config_file=True,
                        help='when training SubTransformer, use --configs to specify architecture and --sub-configs to specify other settings')

    # for profiling
    parser.add_argument('--profile-flops', action='store_true',
                        help='measure the FLOPs of a SubTransformer')

    parser.add_argument('--latgpu', action='store_true',
                        help='measure SubTransformer latency on GPU')
    parser.add_argument('--latcpu', action='store_true',
                        help='measure SubTransformer latency on CPU')
    parser.add_argument('--latiter', type=int, default=300,
                        help='how many iterations to run when measure the latency')
    parser.add_argument('--latsilent', action='store_true',
                        help='keep silent when measure latency')

    parser.add_argument('--validate-subtransformer', action='store_true',
                        help='evaluate the SubTransformer on the validation set')

    parser.add_argument('--ranking-patience', type=int,
                        default=5, help='the patience of ranking')
    parser.add_argument('--corr-threshold', type=float,
                        default=0.9, help='the threshold of ranking correlation')
    parser.add_argument('--rank-list-size', type=int, default=200,
                        help='candidate rank list size for validation')
    parser.add_argument('--loss', type=float, default=0,
                        help='the expected validation loss')
    parser.add_argument('--max-batch', type=int, default=9000,
                        help='the maximum number of batches in an epoch')

    options.add_generation_args(parser)

    args = options.parse_args_and_arch(parser)

    random.seed(args.seed)

    if args.latcpu:
        args.cpu = True
        args.fp16 = False

    if args.latgpu or args.latcpu or args.profile_flops:
        args.distributed_world_size = 1

    if args.pdb:
        pdb.set_trace()

    if args.distributed_init_method is None:
        distributed_utils.infer_init_method(args)

    if args.distributed_init_method is not None:
        # distributed training
        if torch.cuda.device_count() > 1 and not args.distributed_no_spawn:
            start_rank = args.distributed_rank
            args.distributed_rank = None  # assign automatically
            torch.multiprocessing.spawn(
                fn=distributed_main,
                args=(args, start_rank),
                nprocs=torch.cuda.device_count(),
            )
        else:
            distributed_main(args.device_id, args)
    elif args.distributed_world_size > 1:
        # fallback for single node with multiple GPUs
        assert args.distributed_world_size <= torch.cuda.device_count()
        port = random.randint(10000, 20000)
        args.distributed_init_method = 'tcp://localhost:{port}'.format(
            port=port)
        args.distributed_rank = None  # set based on device id
        if max(args.update_freq) > 1 and args.ddp_backend != 'no_c10d':
            print('| NOTE: you may get better performance with: --ddp-backend=no_c10d')
        torch.multiprocessing.spawn(
            fn=distributed_main,
            args=(args,),
            nprocs=args.distributed_world_size,
        )
    else:
        # single GPU training
        main(args)


if __name__ == '__main__':
    cli_main()
