from lightrag.optim.parameter import Parameter, ParameterType
from lightrag.core import Component, Generator
from lightrag.core.generator import BackwardEngine
from lightrag.components.model_client.groq_client import GroqAPIClient
from lightrag.components.model_client.openai_client import OpenAIClient
from lightrag.utils import setup_env
from lightrag.eval.answer_match_acc import AnswerMatchAcc
from lightrag.core import DataClass, fun_to_component
from lightrag.components.output_parsers import YamlOutputParser
from lightrag.optim.text_grad.textual_grad_desc import TextualGradientDescent
from lightrag.optim.text_grad.text_loss_with_eval_fn import EvalFnToTextLoss
from lightrag.optim.text_grad.ops import sum
from lightrag.utils import save_json
from dataclasses import dataclass, field
from textgrad.tasks import load_task
import textgrad as tg
import numpy as np
from typing import Dict, Any, List
import random
import concurrent
from tqdm import tqdm
import logging

logger = logging.getLogger(__name__)

# logger = get_logger(level="DEBUG", filename="adalflow.log")

setup_env()
# Load the data and the evaluation function
llama3_model = {
    "model_client": GroqAPIClient(),
    "model_kwargs": {
        "model": "llama-3.1-8b-instant",
    },
}
gpt_3_model = {
    "model_client": OpenAIClient(input_type="text"),
    "model_kwargs": {
        "model": "gpt-3.5-turbo",
        "max_tokens": 2000,
        "temperature": 0.0,
        "top_p": 0.99,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "stop": None,
    },
}

gpt_4o_model = {
    "model_client": OpenAIClient(),
    "model_kwargs": {
        "model": "gpt-4o",
        "temperature": 0.9,
        "top_p": 0.99,
    },
}


def load_data():
    train_set, val_set, test_set, eval_fn = load_task(
        "BBH_object_counting", evaluation_api=None
    )
    print("Train/Val/Test Set Lengths: ", len(train_set), len(val_set), len(test_set))
    STARTING_SYSTEM_PROMPT = train_set.get_task_description()
    print(STARTING_SYSTEM_PROMPT)


def set_seed(seed):
    np.random.seed(seed)
    random.seed(seed)


@dataclass
class ObjectCountPredData(DataClass):
    thought: str = field(metadata={"desc": "List your step by step reasoning"})
    answer: int = field(
        metadata={"desc": "The answer to the question, only numerical values"}
    )


@fun_to_component
def parse_integer_answer(answer: str, only_first_line: bool = False):
    try:
        if only_first_line:
            answer = answer.strip().split("\n")[0]
        answer = answer.strip()
        # find the last token that has a number in it
        answer = [token for token in answer.split() if any(c.isdigit() for c in token)][
            -1
        ]
        answer = answer.split(".")[0]
        answer = "".join([c for c in answer if c.isdigit()])
        answer = int(answer)

    except (ValueError, IndexError):
        # print(answer)
        answer = 0

    return answer


# Build a pipeline like you normally would == PyTorch model
# TODO: auto saving the prompt and performance.


# 1 Task: with structured output
# 2. use task pipeline instead of a single generator
# 3. train for both output format and the system prompt
class ObjectCountTask(Component):
    def __init__(self, model_client, model_kwargs):
        super().__init__()
        template = r"""<SYS>{{system_prompt}}
        <OUTPUT_FORMAT> {{output_format_str}}</OUTPUT_FORMAT></SYS>
        <USER>{{input_str}}</USER>You:"""  # noqa: F841
        template_2 = r"""<START_OF_SYSTEM_PROMPT>{{system_prompt}}<OUTPUT_FORMAT> {{output_format_str}}</OUTPUT_FORMAT></END_OF_SYSTEM_PROMPT>{{input_str}}"""
        # data = (
        #     "You will answer a reasoning question. Think step by step. The last line of your response should be of the following format: 'Answer: $VALUE' where VALUE is a numerical value.",
        # )
        # 1. set up system prompt, and define the parameters for optimization.
        # NOTE: use self. will double the parameters, so we dont need that as we want the parameter to be part of the generator
        system_prompt = Parameter(
            alias="task_instruction",
            data="You will answer a reasoning question. Think step by step.",
            role_desc="To give task instruction to the language model in the system prompt",
            requires_opt=True,
            param_type=ParameterType.PROMPT,
        )
        instruction = "Do not change the fields in the JSON object. Only improve on the field descriptions."
        output_format_str = Parameter(
            alias="output_format",
            data="Respond with valid JSON object with the following schema:\n"
            + ObjectCountPredData.to_json_signature(),
            role_desc="To specify the LLM output format",
            instruction_to_optimizer=instruction,
            instruction_to_backward_engine=instruction,
            param_type=ParameterType.PROMPT,
            requires_opt=True,
        )
        parser = YamlOutputParser(
            data_class=ObjectCountPredData, return_data_class=True
        )  # noqa: F841
        self.llm_counter = Generator(
            model_client=model_client,
            model_kwargs=model_kwargs,
            template=template_2,
            prompt_kwargs={
                "system_prompt": system_prompt,
                "output_format_str": output_format_str,
            },
            output_processors=parser,
        )
        # TODO: make this data map function more robust (this is the final answer and the input to eval_fn)
        self.llm_counter.set_data_map_func(lambda x: x.data.answer)
        logger.info(f"llm_counter set_data_map_func, {self.llm_counter.data_map_func}")

    # TODO: the error will be a context
    def call(self, question: str) -> Any:  # Union[Parameter, int]:
        output = self.llm_counter(
            prompt_kwargs={"input_str": question}
        )  # already support both training (forward + call)

        if not self.training:  # eval

            if output.data is None:
                logger.error(
                    f"Error in processing the question: {question}, output: {output}"
                )
                output = -1
            else:
                output = output.data.answer
        return output


class ObjectCountTaskOriginal(Component):
    def __init__(self, model_client, model_kwargs):
        super().__init__()
        template = r"""<SYS>{{system_prompt}}
        <OUTPUT_FORMAT> {{output_format_str}}</OUTPUT_FORMAT></SYS>
        <USER>{{input_str}}</USER>You:"""  # noqa: F841
        template_2 = r"""<START_OF_SYSTEM_PROMPT>{{system_prompt}}<OUTPUT_FORMAT> {{output_format_str}}</OUTPUT_FORMAT></END_OF_SYSTEM_PROMPT>{{input_str}}"""
        # data = (
        #     "You will answer a reasoning question. Think step by step. The last line of your response should be of the following format: 'Answer: $VALUE' where VALUE is a numerical value.",
        # )
        # 1. set up system prompt, and define the parameters for optimization.
        # NOTE: use self. will double the parameters, so we dont need that as we want the parameter to be part of the generator
        system_prompt = Parameter(
            alias="task_instruction",
            # data="You will answer a reasoning question. Clearly list each intermediate step before giving the final numerical answer. Double-check each step for accuracy. The last line of your response should be of the following format: 'Answer: $VALUE' where VALUE is a numerical value.",
            data="You will answer a reasoning question. Think step by step. The last line of your response should be of the following format: 'Answer: $VALUE' where VALUE is a numerical value.",
            role_desc="To give task instruction to the language model in the system prompt",
            requires_opt=True,
            param_type=ParameterType.NONE,
        )
        self.llm_counter = Generator(
            model_client=model_client,
            model_kwargs=model_kwargs,
            template=template_2,
            prompt_kwargs={
                "system_prompt": system_prompt,
            },
            output_processors=parse_integer_answer,
        )
        # TODO: make this data map function more robust (this is the final answer and the input to eval_fn)
        # self.llm_counter.set_data_map_func(lambda x: x.data.answer)
        logger.info(f"llm_counter set_data_map_func, {self.llm_counter.data_map_func}")

    # TODO: the error will be a context
    def call(self, question: str) -> Any:  # Union[Parameter, int]:
        output = self.llm_counter(
            prompt_kwargs={"input_str": question}
        )  # already support both training (forward + call)

        if not self.training:  # eval

            if output.data is None:
                logger.error(
                    f"Error in processing the question: {question}, output: {output}"
                )
                output = -1
            else:
                output = int(output.data)
        return output


# Define a evaluator == PyTorch Evaluator
# class ObjectCountEvaluator(BaseEvaluator):


# TODO: improve cache for the training
# Write a trainer  == PyTorch Trainer
class ObjectCountTrainer(Component):
    __doc__ = r"""Text-grad trainer will require:
    - Task pipeline that defines parameters
    - Optimizer and its model parameters
    - Backward engine(to compute gradients) and its model parameters
    """

    def __init__(
        self,
        task_model_config: Dict,
        backward_engine_model_config: Dict,
        tgd_model_config: Dict,
        batch_size: int = 4,
    ):
        super().__init__()
        set_seed(12)
        self.train_set, self.val_set, self.test_set, self.eval_fn = load_task(
            "BBH_object_counting", evaluation_api=None
        )

        self.evaluator = AnswerMatchAcc(type="exact_match")
        self.training_batch_size = batch_size
        print(self.train_set.get_task_description())
        print(f"eval_fn: {self.eval_fn}")
        self.train_loader = tg.tasks.DataLoader(
            self.train_set, batch_size=self.training_batch_size, shuffle=True
        )  # why not torch loader?

        # self.task = ObjectCountTask(**task_model_config)
        self.task = ObjectCountTaskOriginal(**task_model_config)
        # 2. backward engine will be used by all operators
        backward_engine = BackwardEngine(**backward_engine_model_config)
        self.target_params = set(self.task.parameters())

        for param in self.target_params:
            print(f"param: {param.alias}")

        # 3. optimizer will be used to optimize the parameters
        self.optimizer = TextualGradientDescent(
            params=self.target_params,
            **tgd_model_config,
            # constraints=[
            #     "Do not stray too far from the original value.",
            #     "Do not be too specific to the training data to adapt to new data.",
            #     "keep the initial instruction's purpose.",
            # ],
        )

        self.task.llm_counter.set_backward_engine(backward_engine)

        # 4. loss function will be used to compute the loss

        # TODO: set backward_engine should be recursive
        # pred_answer: object, gt_answer: object for compute_single_item
        self.loss_fn = EvalFnToTextLoss(
            eval_fn=self.evaluator.compute_single_item,
            eval_fn_desc="ObjectCountingEvalFn, Output accuracy score: 1 for correct, 0 for incorrect",  # NOTE: important to explain to optimizer what the metric mean.
            backward_engine=backward_engine,
        )

    def test_train(self, start_val_acc: float, start_test_acc: float, max_samples=20):
        # TODO: save a best prompt or top 2
        r"""Test a single training step"""
        self.task.train()
        self.optimizer.zero_grad()
        logger.info(f"Training started: {self.task.training}")
        results = {
            "val_acc": [start_val_acc],  # by step
            "test_acc": [start_test_acc],  # by epoch
            "prompts": [],  # list of dict
        }
        print(f"results: {results}")
        parameters = list(self.task.parameters())  # or use named parameters
        max_steps = 5
        max_samples = max_samples
        save_result_file_path = (
            f"results_adalflow_max_steps_{max_steps}_max_samples_{max_samples}.json"
        )
        for steps, (batch_x, batch_y) in enumerate(
            (pbar := tqdm(self.train_loader, position=0))
        ):
            pbar.set_description(f"Training Step: {steps}")
            self.task.train()

            losses: List[Parameter] = []
            for i, (x, y) in enumerate(zip(batch_x, batch_y)):
                # compute loss on one data point
                logger.info(f"x: {x}, y: {y}")
                response = self.task.call(
                    question=Parameter(
                        data=x,
                        role_desc="query to the language model",
                        requires_opt=False,
                        alias=f"x_{i}",
                    )
                )
                logger.info(f"response: {response}")
                response.alias += f"_{i}"
                # TODO: when it is train, need to pass the data to be something used for eval.
                loss = self.loss_fn(
                    kwargs={
                        "y": response,
                        "y_gt": Parameter(
                            data=y,
                            role_desc="The ground truth",
                            requires_opt=False,
                            alias=f"y_{i}",
                        ),
                    }
                )
                # loss.backward()
                loss.alias += f"_step_{steps}_batch_{i}"
                print(f"y_gt: {y})")
                losses.append(loss)
                # loss.draw_graph(filepath="loss1")

            total_loss = sum(losses)
            # print(f"loss dict: {loss.to_dict()}")
            print("loss backward...")
            total_loss.backward()
            print("optimizer propose...")
            self.optimizer.propose()
            prompts = {p.alias: p.data for p in parameters if p.requires_opt}
            print(f"new prompt: {prompts}")
            # total_loss.draw_graph(filepath=f"total_loss_step_{steps}")
            print("Start evaluate")

            # save_json(total_loss.to_dict(), "total_loss_adalflow.json")

            eval_acc, eval_acc_list = self.evaluate_dataset(
                dataset_type="val", max_samples=max_samples
            )
            print(f"val_acc: {eval_acc}, last acc: {results['val_acc'][-1]}")
            if eval_acc > results["val_acc"][-1]:
                print("optimizer step")
                self.optimizer.step()
                results["val_acc"].append(eval_acc)

            else:
                self.optimizer.revert()
                print("optimizer revert")
                results["val_acc"].append(results["val_acc"][-1])
            final_prompts = {p.alias: p.data for p in parameters if p.requires_opt}
            results["prompts"].append(final_prompts)

            test_acc, test_acc_list = self.evaluate_dataset(
                dataset_type="test", max_samples=max_samples
            )
            results["test_acc"].append(test_acc)
            print(f"test_acc: {test_acc}")

            save_json(results, save_result_file_path)
            if steps >= max_steps:
                break

            # if steps % test_steps == 0:

        # save_json(results, "results_adalflow.json")
        # test only on one epoch, not on each step
        # self.optimizer.step()

    def train(self, max_epochs: int = 1):
        # set it to train mode
        self.task.train()
        for epoch in range(max_epochs):
            pbar = tqdm(self.train_loader, position=0, desc=f"Epoch: {epoch}")
            for steps, (batch_x, batch_y) in enumerate(pbar):
                pbar.set_description(f"Epoch: {epoch}, Step: {steps}")
                self.optimizer.zero_grad()
                for x, y in zip(batch_x, batch_y):
                    response = self.task.call(
                        question=Parameter(
                            data=x,
                            role_desc="query to the language model",
                            requires_opt=False,
                        )
                    )
                    print(f"response: {response}")

    def eval_no_concurrent(self, dataset=None, max_samples: int = 100):
        if dataset is None:
            print("No dataset provided, using test set")
            dataset = self.test_set

        # set it to eval mode
        self.training = False
        x, y, y_pred = [], [], []
        tqdm_loader = tqdm(dataset)
        for _, sample in enumerate(tqdm_loader):
            y.append(sample[1])
            y_pred.append(self.task.call(question=sample[0]))
            x.append(sample[0])
            print(f"y: {y}, y_pred: {y_pred}, x: {x}")
            tqdm_loader.set_description(
                f"Accuracy: {self.evaluator.compute(y_pred, y)}"
            )

        return self.evaluator.compute(y_pred, y)[1]

    def evaluate_dataset(self, dataset_type: str = "test", max_samples: int = 100):

        # set it to eval mode
        dataset = None
        if dataset_type == "test":
            dataset = self.test_set
        elif dataset_type == "val":
            dataset = self.val_set
        elif dataset_type == "train":
            dataset = self.train_set
        else:
            raise ValueError(f"dataset_type: {dataset_type} not supported")

        self.task.eval()
        logger.debug(
            f"{self.__class__.__name__}: trainer eval stage on {dataset_type} dataset"
        )
        x, y, y_pred = [], [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = []
            for _, sample in enumerate(tqdm(dataset)):
                future = executor.submit(self.task.call, question=sample[0])
                futures.append((future, sample))  # store the sample with the future
                if max_samples and len(futures) >= max_samples:
                    break
            tqdm_loader = tqdm(
                concurrent.futures.as_completed(
                    [f[0] for f in futures]
                ),  # Pass only the futures to as_completed
                total=len(futures),
                position=0,
                desc="Evaluating",
            )
            for future in tqdm_loader:
                # Find the associated sample for the future
                associated_sample = next(
                    sample for fut, sample in futures if fut == future
                )
                y.append(associated_sample[1])
                y_pred.append(future.result())
                x.append(associated_sample[0])

                tqdm_loader.set_description(
                    f"{dataset_type} Accuracy: {self.evaluator.compute(y_pred, y)[0]}"
                )
                # print(f"y: {y}, y_pred: {y_pred}, x: {x}")
        return self.evaluator.compute(y_pred, y)  # acc and acc_list

    def _extra_repr(self) -> str:
        s = f"train_set: {len(self.train_set)}, val_set: {len(self.val_set)}, test_set: {len(self.test_set)}, "
        s += f"eval_fn: {self.eval_fn}, "
        s += f"evaluator: {self.evaluator}"
        return s


# TODO: implement cache for generator(make it configurable)
if __name__ == "__main__":
    # task = ObjectCountTask(**gpt_3_model)
    # logger = get_logger(level="DEBUG")
    # print(task)
    # print(
    #     task.llm_counter.print_prompt(
    #         input_str="How many musical instruments do I have?"
    #     )
    # )
    # print(
    #     task.call(
    #         "I have a flute, a piano, a trombone, four stoves, a violin, an accordion, a clarinet, a drum, two lamps, and a trumpet. How many musical instruments do I have?"
    #     )
    # )

    trainer = ObjectCountTrainer(
        task_model_config=gpt_3_model,
        backward_engine_model_config=gpt_4o_model,
        tgd_model_config=gpt_4o_model,
    )
    # print(trainer)
    test_acc, test_acc_list = trainer.evaluate_dataset(
        dataset_type="test", max_samples=None
    )
    print(f"test_acc: {test_acc}")
    val_acc, val_acc_list = trainer.evaluate_dataset(
        dataset_type="val", max_samples=None
    )
    print(f"val_acc: {val_acc}")
    trainer.test_train(start_val_acc=val_acc, start_test_acc=test_acc, max_samples=100)
    # test_acc, test_acc_list = trainer.evaluate_dataset(
    #     dataset_type="test", max_samples=None
    # )
    # print(f"test_acc after optimization: {test_acc}")
    # TODO: use cache for the generator
    #
    # output = trainer.eval(dataset=trainer.val_set, max_samples=5)
    # print(f"eval output: {output}")
    # gpt-3.5-turbo test 0.69 [10 samples = 0.8], 0.72 (simple pasing, instead of json)
    # 0.73 with new parameters close to text-grad, using separate prompt: 0.81
    # single prompt without you: -> 0.82 <SYSTEM> system prompt.<SYS>0.78 <START_OF_SYSTEM_PROMPT> system prompt.<END_OF_SYSTEM_PROMPT> =>0.84 json_output = 0.68
    # yaml parser = 0.73  # json fixed 0.8 with different field description
    # text/ user role -> 0.76
    # so there is performance drop if we put the same prompt together
    # gpt-4o test 0.94

    # eval: 0.8
    # trainer.train(max_epochs=1)
