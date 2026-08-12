import os


def list_files_in_directory():
    files_info = []
    for file_name in os.listdir():
        if os.path.isfile(file_name):
            file_info = {
                "name": file_name,
                "size": os.path.getsize(file_name)
            }
            files_info.append(file_info)
    print(files_info)
    return files_info

# Running the function
list_files_in_directory()
