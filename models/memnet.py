import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from torch.autograd import Variable


class MemNet(nn.Module):
    def __init__(self, settings, model_pretrained):
        super(MemNet, self).__init__()
        self.name_model = 'mem'

        # parameters
        self.use_cuda = settings["use_cuda"]
        self.dim_embedding_key = settings["dim_embedding_key"]
        self.num_prediction = settings["num_prediction"]

        # similarity criterion
        self.similarity = nn.CosineSimilarity(dim=1)

        # Memory
        self.memory_past = torch.Tensor().cuda()
        self.memory_fut = torch.Tensor().cuda()
        self.memory_count = []

        # layers
        self.conv_past = model_pretrained.conv_past
        self.conv_fut = model_pretrained.conv_fut

        self.encoder_past = model_pretrained.encoder_past
        self.encoder_fut = model_pretrained.encoder_fut
        self.decoder = model_pretrained.decoder
        self.FC_output = model_pretrained.FC_output
        self.relu = nn.ReLU()
        self.softmax = nn.Softmax()

        # scene
        # input shape (batch, classes, 360, 360)
        self.convScene_1 = nn.Sequential(
            nn.Conv2d(4, 16, kernel_size=5, stride=2, padding=2),
            nn.ReLU())
        self.convScene_2 = nn.Sequential(
            nn.Conv2d(16, 32, kernel_size=5, stride=1, padding=2),
            nn.ReLU())

        self.RNN_scene = nn.GRU(32, self.dim_embedding_key, 1, batch_first=True)

        # refine layer Fc
        self.fc_refine = nn.Linear(self.dim_embedding_key, 80)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.kaiming_normal_(self.RNN_scene.weight_ih_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_hh_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_ih_l0)
        nn.init.kaiming_normal_(self.RNN_scene.weight_hh_l0)
        nn.init.kaiming_normal_(self.convScene_1[0].weight)
        nn.init.kaiming_normal_(self.convScene_2[0].weight)
        nn.init.kaiming_normal_(self.fc_refine.weight)

        nn.init.zeros_(self.RNN_scene.bias_ih_l0)
        nn.init.zeros_(self.RNN_scene.bias_hh_l0)
        nn.init.zeros_(self.RNN_scene.bias_ih_l0)
        nn.init.zeros_(self.RNN_scene.bias_hh_l0)
        nn.init.zeros_(self.convScene_1[0].bias)
        nn.init.zeros_(self.convScene_2[0].bias)
        nn.init.zeros_(self.fc_refine.bias)

    def create_memory(self, past, future):

        # past encoding
        past = torch.transpose(past, 1, 2)
        story_embed = self.relu(self.conv_past(past))
        story_embed = torch.transpose(story_embed, 1, 2)
        output_past, state_past = self.encoder_past(story_embed)

        # future encoding
        future = torch.transpose(future, 1, 2)
        future_embed = self.relu(self.conv_fut(future))
        future_embed = torch.transpose(future_embed, 1, 2)
        output_fut, state_fut = self.encoder_fut(future_embed)

        state_past = state_past.squeeze(0)
        state_fut = state_fut.squeeze(0)

        # memory caso naive
        # self.memory_past = torch.cat((self.memory_past, state_past))
        # self.memory_fut = torch.cat((self.memory_fut, state_fut))
        self.memory_count = []

        # memory con eliminazione di quelle simili
        i = 0
        if len(self.memory_past) == 0:
            self.memory_past = torch.cat((self.memory_past, state_past[0])).unsqueeze(0)
            self.memory_fut = torch.cat((self.memory_fut, state_fut[0])).unsqueeze(0)
            i += 1

        for i in range(len(past)):

            dim_mem = self.memory_past.shape[0]
            temp_state_past_i = state_past[i].unsqueeze(0).repeat(dim_mem, 1)
            temp_state_fut_i = state_fut[i].unsqueeze(0).repeat(dim_mem, 1)

            sim_mem_past = self.similarity(temp_state_past_i, self.memory_past)
            sim_mem_fut = self.similarity(temp_state_fut_i, self.memory_fut)

            th_past = np.where(sim_mem_past.cpu() == 1.0)
            th_fut = np.where(sim_mem_fut.cpu() == 1.0)

            if len(np.intersect1d(th_past, th_fut)) == 0:
                self.memory_past = torch.cat((self.memory_past, state_past[i].unsqueeze(0)), 0)
                self.memory_fut = torch.cat((self.memory_fut, state_fut[i].unsqueeze(0)), 0)

    def forward(self, past, scene):

        dim_batch = past.size()[0]
        weight_read = torch.FloatTensor(dim_batch, len(self.memory_past)).cuda()
        zero_padding = torch.zeros(1, dim_batch, self.dim_embedding_key * 2).cuda()
        prediction = torch.Tensor().cuda()
        present_temp = past[:, -1].unsqueeze(1)

        # temporal encoding for past
        past = torch.transpose(past, 1, 2)
        story_embed = self.relu(self.conv_past(past))
        story_embed = torch.transpose(story_embed, 1, 2)
        output_past, state_past = self.encoder_past(story_embed)

        # scene encoding
        scene = scene.permute(0, 3, 1, 2)
        scene_1 = self.convScene_1(scene)
        scene_2 = self.convScene_2(scene_1)

        for i in range(dim_batch):
            weight_read[i] = self.similarity(self.memory_past, state_past[:, i]).unsqueeze(0)

        # weight_read[torch.arange(dim_batch)] = self.similarity(self.memory_past, state_past[:,torch.arange(dim_batch)]).unsqueeze(0)
        index_max = torch.sort(weight_read, descending=True)[1].cpu()

        for i_track in range(self.num_prediction):
            # info_total = torch.cat((info_future[:,i_track], story_embed), 1)

            present = present_temp
            prediction_single = torch.Tensor().cuda()
            ind = index_max[:, i_track]
            # +1 nelle celle delle k traiettorie più probabili
            self.memory_count[ind] += 1

            info_future = self.memory_fut[ind]
            info_total = torch.cat((state_past, info_future.unsqueeze(0)), 2)
            input_dec = info_total
            state_dec = zero_padding
            for i in range(40):
                output_decoder, state_dec = self.decoder(input_dec, state_dec)
                displacement_next = self.FC_output(output_decoder)
                coords_next = present + displacement_next.squeeze(0).unsqueeze(1)
                prediction_single = torch.cat((prediction_single, coords_next), 1)
                present = coords_next
                input_dec = zero_padding

            # aggiungere ciclo refinement
            for i_refine in range(4):
                pred_map = prediction_single + 90
                pred_map = pred_map.unsqueeze(2)
                indices = pred_map.permute(0, 2, 1, 3)

                # between -1 and 1
                indices = 2 * (indices / 180) - 1
                output = F.grid_sample(scene_2, indices, mode='nearest')
                output = output.squeeze(2).permute(0, 2, 1)

                # input_rnn = scene_feat
                state_rnn = state_past
                output_rnn, state_rnn = self.RNN_scene(output, state_rnn)
                prediction_refine = self.fc_refine(state_rnn).view(dim_batch, 40, 2)

                prediction_single = prediction_single + prediction_refine

            prediction = torch.cat((prediction, prediction_single.unsqueeze(1)), 1)

        return prediction
