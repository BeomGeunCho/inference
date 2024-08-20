import argparse
import array
import os
from typing import Dict, List, Tuple

import mlperf_loadgen as lg
import torch
from accelerate import disk_offload
from backend_PyTorch import SUT_base as PyTorch_SUT_base
from furiosa_llm_models.gptj.symbolic.huggingface_rope_rngd_gelu import \
    GPTJForCausalLM as upstream_GPTJForCausalLM

from tqdm import tqdm
from transformers import AutoTokenizer
from transformers.generation.logits_process import \
    MinNewTokensLengthLogitsProcessor
from transformers.generation.stopping_criteria import MaxLengthCriteria
from transformers.generation.utils import BeamSearchScorer
from transformers.utils.fx import get_concrete_args

import model_compressor

gen_kwargs = {
    "early_stopping": True,
    "max_new_tokens": 128,
    "min_new_tokens": 30,
    "num_beams": int(
        os.environ.get("GPTJ_BEAM_SIZE", "4")
    ),  # only beam_size 4 is allowed for official submission
}

EARYLY_STOPPING = True
PAD_TOKEN_ID = EOS_TOKEN_ID = 50256
MAX_LENGTH = 2048
MAX_NEW_TOKENS = 128
MIN_NEW_TOKENS = 30
NUM_BEAMS = 4
LENGTH_PENALTY = 1.0
NUM_RETURN_SEQUENCES = 1
RETURN_DICT_IN_GENERATE = False
LOGITS_PROCESSOR = MinNewTokensLengthLogitsProcessor
STOPPING_CRITERIA = MaxLengthCriteria
KV_DTYPE = torch.float32
QUANT_KV_DTYPE = torch.int8
BUCKET_SIZE = 2048
NUM_REAL_BATCH = 1


# TODO: This code should be updated to the latest version of furiosa-llm-models
# Maybe, v3.12.x
class GPTJForCausalLM(upstream_GPTJForCausalLM):
    def get_input_names_and_concrete_args(
        self, model, prefill_phase=True
    ) -> Tuple[List[str], Dict]:
        model = self

        custom_concrete_args = {
            "use_cache": True,
            "return_dict": True,
            "output_attentions": False,
            "output_hidden_states": False,
        }

        if prefill_phase:
            input_names = ["input_ids", "position_ids", "attention_mask"]
        else:
            input_names = ["input_ids", "past_key_values", "position_ids", "attention_mask"]

        concrete_args = get_concrete_args(model, input_names)

        custom_concrete_args = {
            "use_cache": True,
            "return_dict": True,
            "output_attentions": False,
            "output_hidden_states": False,
        }

        for arg in custom_concrete_args:
            if arg in concrete_args:
                concrete_args[arg] = custom_concrete_args[arg]
                continue
            raise ValueError(f"{arg} is not defined in {concrete_args}")

        return input_names, concrete_args

# This is a hack to make the module name of the class to be the same as the original one
GPTJForCausalLM.__module__ = "furiosa_llm_models.gptj.symbolic.huggingface_rope_rngd_gelu"

class SUT_base(PyTorch_SUT_base):
    def __init__(
        self,
        model_path,
        dtype,
        dataset_path,
        scenario,
        max_examples,
        use_gpu=False,
        network=None,
        qsl=None,
        args: argparse.Namespace = None,
    ):
        self.network = network
        self.model_name = "EleutherAI/gpt-j-6B"
        self.model_path = model_path
        self.use_gpu = use_gpu
        self.dataset_path = dataset_path
        self.max_examples = max_examples
        self.scenario = scenario
        self.qsl = qsl
        print("Loading PyTorch model...")

        # dtype
        if dtype == "bfloat16":
            self.amp_enabled = True
            self.amp_dtype = torch.bfloat16
            print("BF16 autocast")
        elif dtype == "float16":
            self.amp_enabled = True
            self.amp_dtype = torch.float16
        else:
            self.amp_enabled = False
            self.amp_dtype = torch.float32
        try:
            self.model = GPTJForCausalLM.from_pretrained(
                self.model_path,
                device_map="auto" if not self.use_gpu else None,
                low_cpu_mem_usage=True if not self.use_gpu else False,
                torch_dtype=self.amp_dtype,
                offload_folder=(
                    "offload" if not self.use_gpu else None
                ),  # specify offload folder when using devices with less RAM
                offload_state_dict=(
                    True if not self.use_gpu else False
                ),  # to have some shards of the model to be on the disk
            )
        except ValueError as e:
            if "disk_offload" in str(e):
                print("Offloading the whole model to disk...")
                self.model = GPTJForCausalLM.from_pretrained(
                    self.model_path,
                    low_cpu_mem_usage=True if not self.use_gpu else False,
                    torch_dtype=self.amp_dtype,
                    offload_state_dict=True if not self.use_gpu else False,
                ).cpu()
                disk_offload(model=self.model, offload_dir="offload")

        # Cast the model to GPU if the flag is set.
        if self.use_gpu:
            print(f"Casting models to GPU...")
            assert torch.cuda.is_available(), "torch gpu is not available, exiting..."
            self.device = torch.device("cuda:0")
            self.model.to(self.device)

        self.model.eval()
        try:  # for systems with low ram, the below command gives error as some part is offloaded to disk
            self.model = self.model.to(memory_format=torch.channels_last)
        except:
            pass
        
        if args.quantize:
            from quantization import quantize_model
            from quantization.utils import random_seed, set_optimization

            random_seed()
            set_optimization(args.torch_numeric_optim)

            if not args.gpu:
                raise ValueError(
                    "Inference on a device other than GPU is not supported yet."
                )
            model_type=type(self.model)
            traced_model = self.model.trace_all()
        
            input_names = {
            "prefill_input_names": traced_model["prefill"].input_names,
            "decode_input_names": traced_model["decode"].input_names,
            }
            concrete_args = {
            "prefill_concrete_args": traced_model["prefill"].concrete_args,
            "decode_concrete_args": traced_model["decode"].concrete_args,
            }

            model= quantize_model(traced_model, qparam_path=args.quant_param_path, qformat_path=args.quant_format_path)
            self.kv_dtype = QUANT_KV_DTYPE
        else:
            model = self.model.trace_all()
            self.kv_dtype = KV_DTYPE

        quant_model ={
            'prefill_model': model['prefill'], 
            'decode_model': model['decode'],
            }
        self.generator = model_compressor.helper.QuantCausalLM(quant_model, model_type, input_names, concrete_args)
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_name,
            model_max_length=1919,
            padding_side="left",
            use_fast=False,
        )
        self.tokenizer.pad_token = self.tokenizer.eos_token

        # calculate the memory size taken by the model
        self.total_mem_size = 0
        parameters = list(self.model.parameters())
        for param in tqdm(parameters):
            self.total_mem_size += param.numel() * param.element_size()
        self.total_mem_size = self.total_mem_size / (1024**3)

        # construct SUT
        self.sut = lg.ConstructSUT(self.issue_queries, self.flush_queries)

    def issue_queries(self, query_samples):
        print("Number of Samples in query_samples : ", len(query_samples))

        total_samples_done = 0
        list_prompts_tokens = []
        list_prompts_attn_masks = []

        # Pass each query to inference_call functiontotal_samples_done
        # Activates only when scenario is Offline and network mode is None
        for i in tqdm(range(len(query_samples))):
            index = query_samples[i].index
            input_ids_tensor = self.qsl.data_object.source_encoded_input_ids[index]
            input_masks_tensor = self.qsl.data_object.source_encoded_attn_masks[index]
            text = self.qsl.data_object.sources[index]
            query = {
                "input_text": text,
                "input_ids_tensor": input_ids_tensor.tolist(),
                "input_masks_tensor": input_masks_tensor.tolist()
            }
            self.inference_call(query, query_samples[i].id)

    def inference_call(self, query, query_id=None):
        ''' Common for all scenarios '''
        torch_device_type = 'cuda' if self.use_gpu else 'cpu'

        input_ids_tensor = torch.tensor(query["input_ids_tensor"])
        input_masks_tensor = torch.tensor(query["input_masks_tensor"])

        # Moves the tensor to CPU or GPU as per argument passed by user
        input_ids_tensor = input_ids_tensor.to(torch_device_type)
        input_masks_tensor = input_masks_tensor.to(torch_device_type)               

            
        with torch.inference_mode(), torch.autocast(device_type=torch_device_type, enabled=self.amp_enabled, dtype=self.amp_dtype if self.amp_enabled else None):
            input_batch = dict()
            input_batch['input_ids'] = input_ids_tensor
            input_batch['attention_mask'] = input_masks_tensor

            output_batch = self.generator.generate(# takes Long and Int tensor datatype only
                **input_batch, **gen_kwargs, pad_token_id=self.tokenizer.eos_token_id)

            input_batch_lengths = [x.shape[0]
                                   for x in input_batch["input_ids"]]

            output_batch_lengths = [x.shape[0] for x in output_batch]

            output_batch_truncated = []
            for data, source_len in zip(output_batch, input_batch_lengths):
                output_batch_truncated.append(data[source_len:])

            output_batch_truncated = torch.stack(output_batch_truncated)
            
            # Loadgen monitors the reponse in corresponding functions
            if ((self.scenario == "SingleStream" or self.scenario == "Server") and self.network == None):
                return output_batch_truncated

            pred_output_batch = output_batch_truncated.cpu().numpy()

            decoded_outputs = [self.tokenizer.decode(output, skip_special_tokens=True) for output in pred_output_batch]
            response_text = decoded_outputs[0]

            # Loadgen monitors the response in GPT_QDL
            if self.network == "sut":
                return {"pred_output_batch":pred_output_batch.tolist(), "response_text": response_text}

            response_array = array.array("B", pred_output_batch[0].tobytes())
            bi = response_array.buffer_info()
            response = lg.QuerySampleResponse(query_id, bi[0], bi[1])
            lg.QuerySamplesComplete([response])


class SUT_Offline(SUT_base):
    def __init__(
        self,
        model_path,
        dtype,
        dataset_path,
        scenario,
        max_examples,
        use_gpu,
        network,
        qsl,
        args,
    ):
        SUT_base.__init__(
            self,
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
            args,
        )

    """IssueQuery and inference methods implemented in Base class"""


class SUT_Server(SUT_base):
    def __init__(
        self,
        model_path,
        dtype,
        dataset_path,
        scenario,
        max_examples,
        use_gpu,
        network,
        qsl,
        args,
    ):

        SUT_base.__init__(
            self,
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
            args,
        )
        self.sut = lg.ConstructSUT(self.issue_queries, self.flush_queries)
        self.total_samples_done = 0

    def issue_queries(self, query_samples):
        # The issue queries function is called multiple times by the loadgen as per Poisson Distribution
        index = query_samples[0].index
        input_ids_tensor = self.qsl.data_object.source_encoded_input_ids[index]
        input_masks_tensor = self.qsl.data_object.source_encoded_attn_masks[index]
        text = self.qsl.data_object.sources[index]
        query = {
            "input_ids_tensor": input_ids_tensor.tolist(),
            "input_masks_tensor": input_masks_tensor.tolist(),
        }
        pred_output_batch = (
            self.inference_call(query, query_samples[0].id).cpu().numpy()
        )
        response_array = array.array("B", pred_output_batch.tobytes())
        bi = response_array.buffer_info()
        responses = [lg.QuerySampleResponse(query_samples[0].id, bi[0], bi[1])]
        lg.QuerySamplesComplete(responses)

        self.total_samples_done += 1
        if self.total_samples_done % 5 == 0:
            print("Completed : ", self.total_samples_done)


class SUT_SingleStream(SUT_base):
    def __init__(
        self,
        model_path,
        dtype,
        dataset_path,
        scenario,
        max_examples,
        use_gpu,
        network,
        qsl,
        args,
    ):
        SUT_base.__init__(
            self,
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
            args,
        )
        self.sut = lg.ConstructSUT(self.issue_queries, self.flush_queries)
        self.total_samples_done = 0

    def issue_queries(self, query_samples):
        # This function is called by the loadgen after completing the previous query
        index = query_samples[0].index
        input_ids_tensor = self.qsl.data_object.source_encoded_input_ids[index]
        input_masks_tensor = self.qsl.data_object.source_encoded_attn_masks[index]
        query = {
            "input_ids_tensor": input_ids_tensor.tolist(),
            "input_masks_tensor": input_masks_tensor.tolist(),
        }

        pred_output_batch = (
            self.inference_call(query, query_samples[0].id).cpu().numpy()
        )

        response_array = array.array("B", pred_output_batch.tobytes())
        bi = response_array.buffer_info()
        responses = [lg.QuerySampleResponse(query_samples[0].id, bi[0], bi[1])]
        lg.QuerySamplesComplete(responses)

        self.total_samples_done += 1
        if self.total_samples_done % 5 == 0:
            print("Completed : ", self.total_samples_done)


def get_SUT(
    model_path,
    scenario,
    dtype,
    dataset_path,
    max_examples,
    use_gpu=False,
    network=None,
    qsl=None,
    args: argparse.Namespace = None,
):
    if scenario == "Offline":
        return SUT_Offline(
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
            args,
        )
    elif scenario == "Server":
        return SUT_Server(
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
        )
    elif scenario == "SingleStream":
        return SUT_SingleStream(
            model_path,
            dtype,
            dataset_path,
            scenario,
            max_examples,
            use_gpu,
            network,
            qsl,
        )