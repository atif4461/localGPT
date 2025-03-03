import os
import logging
import click
import torch
import utils

import parse_cpp
from parse_cpp import Language
from parse_cpp import Treesitter

from langchain.chains import RetrievalQA
from langchain.embeddings import HuggingFaceInstructEmbeddings
from langchain.llms import HuggingFacePipeline
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler  # for streaming response
from langchain.callbacks.manager import CallbackManager

callback_manager = CallbackManager([StreamingStdOutCallbackHandler()])

from prompt_template_utils import get_prompt_template
from utils import get_embeddings

# from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain.vectorstores import Chroma
from transformers import (
    GenerationConfig,
    pipeline,
)

from load_models import (
    load_quantized_model_awq,
    load_quantized_model_gguf_ggml,
    load_quantized_model_qptq,
    load_full_model,
)

from constants import (
    EMBEDDING_MODEL_NAME,
    PERSIST_DIRECTORY,
    MODEL_ID,
    MODEL_BASENAME,
    MAX_NEW_TOKENS,
    MODELS_PATH,
    CHROMA_SETTINGS,    
)

import re
import glob

# notes
# runTFCSShapeValidation.cxx cpp decorators
# FCS-GPU-Llama-3.3-70B-Instruct-RAGS/FastCaloSimAnalyzer/macro/runTFCSShapeValidation.cxx bad message
# dir_path = r'/home/atif/localGPT/firstPCA.cxx' # need to use latin-1 encoding for diacritics
# 
# Currently reading files larger than 2 lines
# Added a watermark statement for LLM comment generation
# Skip functions with existing comments, or Rand4Hits_hip.cxx
# dir_path = r'/home/atif/localGPT/TFCS1DFunctionFactory.cxx' # treesitter skips 1st functions due to file level comment block

def extract_cpp_functions(file_path):
    
    functions = []
    print(f'Collecting functions in {file_path}', flush=True)
    with open(file_path, "r", encoding="latin-1") as file:
        # Read the entire content of the file into a string
        file_bytes = file.read().encode()

        file_extension = "cpp" #utils.get_file_extension(file_name)
        programming_language = Language.CPP #utils.get_programming_language(file_extension)

        treesitter_parser = Treesitter.create_treesitter(programming_language)
        treesitterNodes: list[TreesitterMethodNode] = treesitter_parser.parse(
            file_bytes
        )

        for node in treesitterNodes:
            # Count the number of lines in the function
            num_lines = node.method_source_code.count('\n')
            # Add uncommented functions to list
            if node.doc_comment == None and num_lines > 2:
                functions.append(node.method_source_code)

    file.close()
    return functions


def extract_cpp_comments(file_path):
    
    functions_comments = []
    functions = []
    
    with open(file_path, "r", encoding="latin-1") as file:
        # Read the entire content of the file into a string
        file_bytes = file.read().encode()

        file_extension = "cpp" #utils.get_file_extension(file_name)
        programming_language = Language.CPP #utils.get_programming_language(file_extension)

        treesitter_parser = Treesitter.create_treesitter(programming_language)
        treesitterNodes: list[TreesitterMethodNode] = treesitter_parser.parse(
            file_bytes
        )

        for node in treesitterNodes:
            # Count the number of lines in the function
            num_lines = node.method_source_code.count('\n')
            if num_lines > 2:
                first_newline = node.method_source_code.find('\n')
                first_line = node.method_source_code[0:first_newline]
                functions_comments.append(f'{node.doc_comment} \n {first_line}')
                functions.append(f'{first_line}')

    file.close()
    return functions_comments, functions



def remove_comment_block_init_end(answer):
    
    # this is needed as a check to remove any possibility
    # of incompilable hallucination or nested comment blocks
    answer = answer.replace('\n/**\n','')
    answer = answer.replace('/**\n','')
    answer = answer.replace('/**','')
    return answer.replace('*/','')


def rewrite_file_with_comments(functions, file_path, prompt, qa, temperature): 


    if len(functions) > 0:
        file_path_comments = file_path + ".comments.cxx"
        print(f'Generating comments for {len(functions)} functions in {file_path}', flush=True)
 
        functions_first_line = []
        for function in functions:
            #print("FF", function)
            newline_index = function.index('\n')
            line = function[:newline_index]
            functions_first_line.append(line)


        idx = 0
        try:
            with open(file_path, 'r', encoding="latin-1") as source_file:
                with open(file_path_comments, 'w', encoding="utf-8") as destination_file:
                    for line in source_file:
                        # if there are no function definitions
                        if len(functions_first_line) > 0:
                            # if all functions have been read
                            if idx == len(functions_first_line):
                                destination_file.write(line)
                            else:
                                # Strip the lines of trailing/leading whitespaces
                                if line.strip() == functions_first_line[idx].strip():
                                    query = prompt + functions[idx]
                                    res = qa(query)
                                    answer, docs = res["result"], res["source_documents"]
                                    destination_file.write("/**")
                                    clean_answer = remove_comment_block_init_end(answer)
                                    print(f"Going to write \n'{clean_answer}'", flush=True)
                                    destination_file.write(clean_answer)
                                    destination_file.write(f"* This comment was generated by {MODEL_ID}:{MODEL_BASENAME} at temperature {temperature}.\n")
                                    destination_file.write("*/ \n")
                                    destination_file.write(line)
                                    print(f'Function {idx}: {line} Done', flush=True)
                                    idx = idx + 1
                                else:
                                    destination_file.write(line)
                        else:
                            destination_file.write(line)

        except FileNotFoundError:
            print(f"Error: The file {c_file_path} was not found.")
        except IOError as e:
            print(f"Error: An I/O error occurred. {e}")

        # Exchange file names
        #os.rename(file_path, file_path+'.orig')
        os.rename(file_path_comments, file_path)

    else:
        print(f'No functions captured.', flush=True)

def load_model(device_type, model_id, model_basename=None, LOGGING=logging, temperature=0.2):
    """
    Select a model for text generation using the HuggingFace library.
    If you are running this for the first time, it will download a model for you.
    subsequent runs will use the model from the disk.

    Args:
        device_type (str): Type of device to use, e.g., "cuda" for GPU or "cpu" for CPU.
        model_id (str): Identifier of the model to load from HuggingFace's model hub.
        model_basename (str, optional): Basename of the model if using quantized models.
            Defaults to None.

    Returns:
        HuggingFacePipeline: A pipeline object for text generation using the loaded model.

    Raises:
        ValueError: If an unsupported model or device type is provided.
    """
    logging.info(f"Loading Model: {model_id}, on: {device_type}")
    logging.info("This action can take a few minutes!")
    
    if model_basename is not None:
        if ".gguf" in model_basename.lower():
            llm = load_quantized_model_gguf_ggml(model_id, model_basename, device_type, LOGGING)
            return llm
        elif ".ggml" in model_basename.lower():
            model, tokenizer = load_quantized_model_gguf_ggml(model_id, model_basename, device_type, LOGGING)
        elif ".awq" in model_basename.lower():
            model, tokenizer = load_quantized_model_awq(model_id, LOGGING)
        else:
            model, tokenizer = load_quantized_model_qptq(model_id, model_basename, device_type, LOGGING)
    else:
        model, tokenizer = load_full_model(model_id, model_basename, device_type, LOGGING)

    # Load configuration from the model to avoid warnings
    generation_config = GenerationConfig.from_pretrained(model_id)
    # see here for details:
    # https://huggingface.co/docs/transformers/
    # main_classes/text_generation#transformers.GenerationConfig.from_pretrained.returns

    # Create a pipeline for text generation
    if device_type == "hpu":
        from gaudi_utils.pipeline import GaudiTextGenerationPipeline

        pipe = GaudiTextGenerationPipeline(
            model_name_or_path=model_id,
            max_new_tokens=1000,
            temperature=temperature,
            top_p=0.95,
            repetition_penalty=1.15,
            do_sample=True,
            max_padding_length=5000,
        )
        pipe.compile_graph()
    else:
        pipe = pipeline(
        "text-generation",
        model=model,
        tokenizer=tokenizer,
        max_length=MAX_NEW_TOKENS,
        temperature=temperature,
        # top_p=0.95,
        do_sample=True,
        repetition_penalty=1.15,
        generation_config=generation_config,
    )

    local_llm = HuggingFacePipeline(pipeline=pipe)
    logging.info("Local LLM Loaded")

    return local_llm


def retrieval_qa_pipline(device_type, use_history, promptTemplate_type="llama", temperature=0.2):
    """
    Initializes and returns a retrieval-based Question Answering (QA) pipeline.

    This function sets up a QA system that retrieves relevant information using embeddings
    from the HuggingFace library. It then answers questions based on the retrieved information.

    Parameters:
    - device_type (str): Specifies the type of device where the model will run, e.g., 'cpu', 'cuda', etc.
    - use_history (bool): Flag to determine whether to use chat history or not.

    Returns:
    - RetrievalQA: An initialized retrieval-based QA system.

    Notes:
    - The function uses embeddings from the HuggingFace library, either instruction-based or regular.
    - The Chroma class is used to load a vector store containing pre-computed embeddings.
    - The retriever fetches relevant documents or data based on a query.
    - The prompt and memory, obtained from the `get_prompt_template` function, might be used in the QA system.
    - The model is loaded onto the specified device using its ID and basename.
    - The QA system retrieves relevant documents using the retriever and then answers questions based on those documents.
    """

    """
    (1) Chooses an appropriate langchain library based on the enbedding model name.  Matching code is contained within ingest.py.

    (2) Provides additional arguments for instructor and BGE models to improve results, pursuant to the instructions contained on
    their respective huggingface repository, project page or github repository.
    """
    if device_type == "hpu":
        from gaudi_utils.embeddings import load_embeddings

        embeddings = load_embeddings()
    else:
        embeddings = get_embeddings(device_type)

    logging.info(f"Loaded embeddings from {EMBEDDING_MODEL_NAME}")

    # load the vectorstore
    db = Chroma(persist_directory=PERSIST_DIRECTORY, embedding_function=embeddings, client_settings=CHROMA_SETTINGS)
    retriever = db.as_retriever()

    # get the prompt template and memory if set by the user.
    prompt, memory = get_prompt_template(promptTemplate_type=promptTemplate_type, history=use_history)

    # load the llm pipeline
    llm = load_model(device_type, model_id=MODEL_ID, model_basename=MODEL_BASENAME, LOGGING=logging, temperature=temperature)

    if use_history:
        qa = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",  # try other chains types as well. refine, map_reduce, map_rerank
            retriever=retriever,
            return_source_documents=True,  # verbose=True,
            callbacks=callback_manager,
            chain_type_kwargs={"prompt": prompt, "memory": memory},
        )
    else:
        qa = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",  # try other chains types as well. refine, map_reduce, map_rerank
            retriever=retriever,
            return_source_documents=True,  # verbose=True,
            callbacks=callback_manager,
            chain_type_kwargs={
                "prompt": prompt,
            },
        )

    return qa


# chose device typ to run on as well as to show source documents.
@click.command()
@click.option(
    "--device_type",
    default="cuda" if torch.cuda.is_available() else "cpu",
    type=click.Choice(
        [
            "cpu",
            "cuda",
            "ipu",
            "xpu",
            "mkldnn",
            "opengl",
            "opencl",
            "ideep",
            "hip",
            "ve",
            "fpga",
            "ort",
            "xla",
            "lazy",
            "vulkan",
            "mps",
            "meta",
            "hpu",
            "mtia",
        ],
    ),
    help="Device to run on. (Default is cuda)",
)
@click.option(
    "--show_sources",
    "-s",
    is_flag=True,
    help="Show sources along with answers (Default is False)",
)
@click.option(
    "--use_history",
    "-h",
    is_flag=True,
    help="Use history (Default is False)",
)
@click.option(
    "--model_type",
    default="llama3",
    type=click.Choice(
        ["llama3", "llama", "mistral", "non_llama", "deepseek-ai"],
    ),
    help="model type, llama3, llama, mistral or non_llama",
)
@click.option(
    '--temperature', 
    type=float, 
    help='0.0 < temperature <= 1, higher is more creative'
)
@click.option(
    "--save_qa",
    is_flag=True,
    help="whether to save Q&A pairs to a CSV file (Default is False)",
)
def main(device_type, show_sources, use_history, model_type, save_qa, temperature):
    """
    Implements the main information retrieval task for a localGPT.

    This function sets up the QA system by loading the necessary embeddings, vectorstore, and LLM model.
    It then enters an interactive loop where the user can input queries and receive answers. Optionally,
    the source documents used to derive the answers can also be displayed.

    Parameters:
    - device_type (str): Specifies the type of device where the model will run, e.g., 'cpu', 'mps', 'cuda', etc.
    - show_sources (bool): Flag to determine whether to display the source documents used for answering.
    - use_history (bool): Flag to determine whether to use chat history or not.

    Notes:
    - Logging information includes the device type, whether source documents are displayed, and the use of history.
    - If the models directory does not exist, it creates a new one to store models.
    - The user can exit the interactive loop by entering "exit".
    - The source documents are displayed if the show_sources flag is set to True.

    """

    logging.info(f"Running on: {device_type}")
    logging.info(f"Display Source Documents set to: {show_sources}")
    logging.info(f"Use history set to: {use_history}")

    # check if models directory do not exist, create a new one and store models here.
    if not os.path.exists(MODELS_PATH):
        os.mkdir(MODELS_PATH)

    qa = retrieval_qa_pipline(device_type, use_history, promptTemplate_type=model_type, temperature=temperature)
 
    #dir_home = r'/home/atif/FCS-GPU/FastCaloSimAnalyzer/'
    dir_home = r'/home/atif/wire-cell-2dtoy/'

    # Code documentation
    # Generate function level Doxygen style comments
    list_all_files = []
    # search all source files inside a specific folder
    dir_path = dir_home+r'/**/*.cxx'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
    #print(list_all_files)
   
    prompt = "Please read the entire C++ code given below. Generate a Doxygen style comment for each function. Write only the Doxygen style comment using only alphanumeric characters. Do not explain your thinking or write the function name."
    for file_path in list_all_files:

        #print("\n" + "=" *10 + file_path + "=" * 10 + "\n")
        functions = extract_cpp_functions(file_path)
        #print("BBB", functions)
        rewrite_file_with_comments(functions, file_path, prompt, qa, temperature)

    # search all header files inside a specific folder
    list_all_files = []
    dir_path = dir_home+r'/**/*.h'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
    #print(list_all_files)
 
    prompt = "Please read the entire C++ code given below. Generate a Doxygen style comment for each class. Write only the Doxygen style comment using only alphanumeric characters. Do not explain your thinking or write the class name."

    for file_path in list_all_files:

        #print("\n" + "=" *10 + file_path + "=" * 10 + "\n")
        functions = extract_cpp_functions(file_path)
        #print("BBB", functions)
        rewrite_file_with_comments(functions, file_path, prompt, qa, temperature)

    # Code summarization
    # Generate file level summary
    list_all_files = []
    # search all source files inside a specific folder
    dir_path = dir_home+r'/**/*.cxx'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
    
    # search all header files inside a specific folder
    dir_path = dir_home+r'/**/*.h'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
    print(list_all_files)
    
    with open(dir_home+f"/celloai_summary_temperature_{temperature}.txt", 'w', encoding="utf-8") as destination_file:
        destination_file.write(f"This is generated by {MODEL_ID}:{MODEL_BASENAME} at temperature {temperature}.\n")
        for file_path in list_all_files:

            functions_comments, functions = extract_cpp_comments(file_path)
            destination_file.write(f'\n\n *-*-*-*-*-*-*-*-*-*-*-*\n ')
            destination_file.write(f'\n File {file_path} has ')
            for function in functions:
                destination_file.write("\n    ")
                destination_file.write(function)

            source_file = ""
            for function_comment in functions_comments:
                source_file += function_comment
                source_file += "\n\n"
                #print(function_comment)
                #print('\n')

            #print(source_file)

            query = "The following source file contains functions and comments of a C++ program. Write a concise summary for the source file." + source_file
            res = qa(query)

            answer, docs = res["result"], res["source_documents"]
            destination_file.write(answer)
            destination_file.flush()




if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(message)s", level=logging.INFO
    )
    main()
