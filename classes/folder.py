# coding: utf-8

from os.path import isdir, basename

# Classe Folder: défini une carte 'p2/SxS/SD' virtuelle, contenant des objets clips et permettant la convertion en utilisant le bon process
class Folder:
    
    def __init__(self, path):
        self.path = path
        self.name = basename(self.path)

    def validate(self):

        if isdir(self.path):
            return True
        else:
            return False
            
    def getPath(self):
        return self.path