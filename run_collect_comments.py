import os
import logging
import click
import torch
import utils
import re
import glob

import parse_cpp
from parse_cpp import Language
from parse_cpp import Treesitter


def extract_cpp_comments(file_path):
    
    comments = []
    #print(f'Collecting comments in {file_path}')
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
            if num_lines > 2:
                comments.append(node.doc_comment)

    file.close()
    return comments



def main():

    # search all source files inside a specific folder
    list_all_files = []
    dir_path = r'/home/atif/wire-cell-2dtoy/**/*.cxx'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
    #print(list_all_files)
   
    for file_path in list_all_files:
        comments = extract_cpp_comments(file_path)
        for comment in comments:
            print(comment)

    # search all header files inside a specific folder
    list_all_files = []
    dir_path = r'/home/atif/wire-cell-2dtoy/**/*.h'
    for file in glob.glob(dir_path, recursive=True):
        list_all_files.append(file)
 
    for file_path in list_all_files:
        comments = extract_cpp_comments(file_path)
        for comment in comments:
            print(comment)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(message)s", level=logging.INFO
    )
    main()
