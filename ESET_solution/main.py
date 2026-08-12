import os


with os.scandir(".") as directory:
    for entry in directory:
        print(entry.name)
