import torch
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from datautils import MyTrainDataset

import torch.multiprocessing as mp
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group
import os
import argparse

import torch.nn as nn
from transformers import AutoTokenizer
from transformers.models.llama.modeling_llama import LlamaWithLayerWeights
from dataset_mmlu import MultipleChoiceDataset
from dataset_mmlu_concat import MultipleChoiceConcatDataset
from dataset_mmlu_wo_option import MultipleChoiceConcatWODataset
from tqdm import tqdm
from torch.optim import AdamW

def ddp_setup():
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
    init_process_group(backend="nccl")
# def ddp_setup(rank: int, world_size: int):
#    """
#    Args:
#        rank: Unique identifier of each process
#       world_size: Total number of processes
#    """
#    os.environ["MASTER_ADDR"] = "localhost"
#    os.environ["MASTER_PORT"] = "12355"
#    torch.cuda.set_device(rank)
#    init_process_group(backend="nccl", rank=rank, world_size=world_size)

def sync_across_gpus(t, world_size):
    """
    This function synchronizes the tensor 't' across all GPUs and returns
    the concatenated result from all GPUs.
    """
    # Create a list of tensors to gather the results from each GPU
    gather_t_tensor = [torch.zeros_like(t) for _ in range(world_size)]
    
    # Gather all tensors from all GPUs
    dist.all_gather(gather_t_tensor, t)
    
    # Concatenate the results along the first dimension
    return torch.cat(gather_t_tensor, dim=0)

def compute_loss(logits, labels):
    criterion = nn.NLLLoss()
    loss = criterion(logits, labels)
    return loss

def compute_accuracy(logits, labels):
    _, predicted = torch.max(logits, 1)
    correct = (predicted == labels).sum().item()
    accuracy = correct / labels.size(0)
    return accuracy

class Trainer:
    def __init__(
        self,
        model: torch.nn.Module,
        train_data: DataLoader,
        optimizer: torch.optim.Optimizer,
        save_every: int,
        save_folder: str,
        data_folder: str,
        subject: str,
        lr: float,
        method: str
    ) -> None:
        self.gpu_id = int(os.environ["LOCAL_RANK"])
        self.model = model.to(self.gpu_id)
        self.train_data = train_data
        self.optimizer = optimizer
        self.save_every = save_every
        self.epochs_run = 0
        self.save_folder = save_folder
        self.data_folder = data_folder
        self.subject = subject
        self.lr = lr 
        self.method = method
        
        if self.save_folder:
            os.makedirs(save_folder, exist_ok=True)
        
        self.model = DDP(self.model, device_ids=[self.gpu_id])

    def _load_snapshot(self, snapshot_path):
        loc = f"cuda:{self.gpu_id}"
        snapshot = torch.load(snapshot_path, map_location=loc)
        self.model.load_state_dict(snapshot["MODEL_STATE"])
        self.epochs_run = snapshot["EPOCHS_RUN"]
        print(f"Resuming training from snapshot at Epoch {self.epochs_run}")

    def _run_batch(self, input_ids, attention_mask, label, method, epoch, pre_len=None):
        if method == 'letter':
            self.optimizer.zero_grad()
            out_idxs = []
            for i in range(attention_mask.size(0)):
                out_idx = ((attention_mask[i] != 1).nonzero(as_tuple=True)[0])[0].item() - 1
                out_idxs.append(out_idx)
        
            outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        
            out_idxs = torch.tensor(out_idxs).to(self.gpu_id)
            out_idxs = out_idxs.unsqueeze(1)
              
            logits = outputs.logits.gather(1, out_idxs.unsqueeze(-1).expand(-1, -1, outputs.logits.size(-1)).long())
            logits = logits.squeeze(1)
            logits = logits[:, [319, 350, 315, 360]]
                
            logits = logits.to(self.gpu_id)
            loss = compute_loss(logits, label)
            print(f"Epoch {epoch}, Batch Loss: {loss.item()}")
            loss.backward()
            self.optimizer.step()
        if args.method in ['concat', 'wo_option']:
            self.optimizer.zero_grad()
            # batch
            all_batch = []
            for i in range(attention_mask.size(0)):
                input_ids_sample = input_ids[i]
                attention_mask_sample = attention_mask[i]
                prefix_len = pre_len[i]
                answer_ids = input_ids_sample[:, prefix_len-1:]
                real_answer_ids = []
                for row in answer_ids:
                    idx = (row != 2).nonzero(as_tuple=True)[0].max().item()
                    row = row[:idx+1]
                    real_answer_ids.append(row)

                outputs = self.model(input_ids=input_ids_sample, attention_mask=attention_mask_sample)

                # option
                one_batch = []
                for j in range(attention_mask_sample.size(0)):
                    num = len(real_answer_ids[j])
                    start_idx = pre_len[i].to(self.gpu_id)
                    idx_range = torch.arange(num).unsqueeze(0).expand(1, num).to(device)
                    start_idx_tensor = start_idx.clone().unsqueeze(0).expand(1, num) - 2
                    final_idx = start_idx_tensor + idx_range
                    
                    logits_selected = outputs.logits[j][final_idx]
                    real_answer_ids[j] = real_answer_ids[j].unsqueeze(0)
                    logits_selected = logits_selected[0, torch.arange(num), real_answer_ids[j].squeeze(0)]
                    logits_selected = logits_selected.sum()
                    # logits_selected = logits_selected.sum() / num
                    one_batch.append(logits_selected)
                one_batch = torch.stack(one_batch)
                all_batch.append(one_batch)
            all_batch = torch.stack(all_batch, dim=0)
            logits = all_batch.to(self.gpu_id)
            loss = compute_loss(logits, label)
            loss.backward()
            print(f"Epoch {epoch+1}, Batch Loss: {loss.item()}")
            self.optimizer.step()

    def _run_epoch(self, epoch, method):
        b_sz = len(next(iter(self.train_data))[0])
        print(f"[GPU{self.gpu_id}] Epoch {epoch} | Batchsize: {b_sz} | Steps: {len(self.train_data)}")
        self.train_data.sampler.set_epoch(epoch)
        for batch in self.train_data:
            input_ids = batch["input_ids"].to(self.gpu_id)
            attention_mask = batch["attention_mask"].to(self.gpu_id)
            label = batch["label"].to(self.gpu_id)
            if method == 'letter':
                self._run_batch(input_ids, attention_mask, label, method, epoch)
            if method == 'wo_option':
                pre_len = batch["prefix_ids_len"].to(self.gpu_id)
                self._run_batch(input_ids, attention_mask, label, method, epoch, pre_len)

    def _save_snapshot(self, epoch):
        if self.save_folder:
            torch.save(self.model.module.layer_weights.data, os.path.join(self.save_folder, f"{self.subject}_epoch{epoch}.pth"))
        print(f"End of Epoch {epoch}, Layer Weights:", self.model.module.layer_weights.data)

    def train(self, max_epochs: int, method: str):
        self._test(method)
        for epoch in range(self.epochs_run, max_epochs):
            self._run_epoch(epoch, method)
            if self.gpu_id == 0 and epoch % self.save_every == 0 and self.save_folder:
                self._save_snapshot(epoch)
            self._test(method)

    def _test(self, method):
        total_correct = 0
        total_samples = 0
        for batch in self.train_data:
            self.optimizer.zero_grad()
            input_ids = batch["input_ids"].to(self.gpu_id)
            attention_mask = batch["attention_mask"].to(self.gpu_id)
            label = batch["label"].to(self.gpu_id)

            out_idxs = []
            for i in range(attention_mask.size(0)):
                out_idx = ((attention_mask[i] != 1).nonzero(as_tuple=True)[0])[0].item() - 1
                out_idxs.append(out_idx)
    
            outputs = self.model(input_ids=input_ids, attention_mask=attention_mask)
        
            out_idxs = torch.tensor(out_idxs).to(self.gpu_id)
            out_idxs = out_idxs.unsqueeze(1)
              
            logits = outputs.logits.gather(1, out_idxs.unsqueeze(-1).expand(-1, -1, outputs.logits.size(-1)).long())
            logits = logits.squeeze(1)
            logits = logits[:, [319, 350, 315, 360]]
            logits = logits.to(self.gpu_id)
            batch_accuracy = compute_accuracy(logits, label)
            total_correct += (batch_accuracy * label.size(0))
            total_samples += label.size(0)

        epoch_accuracy = total_correct / total_samples
        print(f"Accuracy: {epoch_accuracy * 100:.2f}%")

    def _eval(model, dataloader, world_size):
    """
    This function evaluates the model and computes the accuracy across multiple GPUs.
    It synchronizes the results across all GPUs using `sync_across_gpus`.
    """
    results = torch.tensor([]).cuda()  # Initialize an empty tensor to store results on the current GPU

    # Set the model to evaluation mode
    model.eval()

    with torch.no_grad():  # Disable gradient computation during evaluation
        for step, (inputs, labels) in enumerate(dataloader):
            inputs, labels = inputs.cuda(), labels.cuda()  # Move data to GPU

            # Forward pass: get model outputs
            outputs = model(inputs)

            # Calculate the accuracy for this batch
            res = (outputs.argmax(-1) == labels)  # True for correct predictions, False for incorrect
            results = torch.cat([results, res], dim=0)  # Concatenate results across batches

    # Synchronize results across all GPUs
    results = sync_across_gpus(results, world_size)

    # Calculate the mean accuracy
    mean_acc = (results.sum() / len(results)).item()  # Compute the average accuracy

    return mean_acc



def load_train_objs(model_path, data_folder, subject, lr, method):
   
    ### 这里用的测试集训练
    model = LlamaWithLayerWeights.from_pretrained(model_path)
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if tokenizer.pad_token is None:
       tokenizer.pad_token = tokenizer.eos_token
    
    train_file = os.path.join(os.path.join(data_folder, 'test'), subject + '_test.csv')
    val_file = os.path.join(os.path.join(data_folder, 'val'), subject + '_val.csv')
    if method == 'letter':
        dataset = MultipleChoiceDataset(subject, train_file, val_file, tokenizer)
    if method == 'wo_option':
        dataset = MultipleChoiceConcatWODataset(subject, train_file, val_file, tokenizer)
    
    optimizer = AdamW([model.layer_weights], lr=lr)
    
    return dataset, model, optimizer, tokenizer


def prepare_dataloader(dataset: Dataset, batch_size: int):
    return DataLoader(
        dataset,
        batch_size=batch_size,
        pin_memory=True,
        shuffle=False,
        sampler=DistributedSampler(dataset)
    )


# def main(save_every: int, total_epochs: int, batch_size: int, model: str, data_folder: str, subject: str, lr: float, save_folder: str, method: str):
#     ddp_setup()
#     dataset, model, optimizer, tokenizer = load_train_objs(model, data_folder, subject, lr, method)
    
#     for param in model.parameters():
#         param.requires_grad = False
#     model.module.layer_weights.requires_grad = True
    
#     train_data = prepare_dataloader(dataset, batch_size)
#     trainer = Trainer(model, train_data, optimizer, save_every, save_folder, data_folder, subject, lr, method)
#     trainer.train(total_epochs, method)
#     destroy_process_group()

def main(rank, world_size, total_epochs, save_every, model, data_folder, subject, lr, save_folder, method, batch_size):
    ddp_setup(rank, world_size)
    dataset, model, optimizer, tokenizer = load_train_objs(model, data_folder, subject, lr, method)
    train_data = prepare_dataloader(dataset, batch_size)
    trainer = Trainer(model, train_data, optimizer, save_every, save_folder, data_folder, subject,lr,method)
    trainer.train(total_epochs, method)
    destroy_process_group()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='simple distributed training job')
    parser.add_argument('--total_epochs', type=int, help='Total epochs to train the model')
    parser.add_argument('--save_every', type=int, help='How often to save a snapshot')
    parser.add_argument('--batch_size', default=4, type=int, help='Input batch size on each device (default: 32)')
    
    parser.add_argument('--model', type=str)
    parser.add_argument('--data_folder', type=str)
    parser.add_argument('--subject', type=str)
    parser.add_argument('--lr', type=float)
    parser.add_argument('--save_folder', type=str)
    parser.add_argument('--method', type=str)
    args = parser.parse_args()

    main(args.save_every, args.total_epochs, args.batch_size, args.model, args.data_folder, args.subject, args.lr, args.save_folder, args.method)
    # world_size = torch.cuda.device_count()
    # mp.spawn(main, args=(world_size, args.total_epochs, args.save_every, args.model, args.data_folder, args.subject, args.lr, args.save_folder, args.method, args.batch_size), nprocs=world_size)
