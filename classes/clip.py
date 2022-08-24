# coding: utf-8
STATUS_NOT_IMPORTED = 0
STATUS_COPIED = 1
STATUS_PLACEHOLDER_CREATED = 2
STATUS_IMPORTED = 3

# Classe Clip: défini un clip avec les metadatas issues de la camera
class Clip:
    def __init__(self, umid):
        self.umid = umid
        self.name = ""
        self.timecode = "00:00:00:00"
        self.duration = "0"
        self.metadatas = {}
        self.input_files = []
        self.output_file = ""
        self.provider = None
        self.spanned = False
        self.spanned_with = ""
        self.spanned_clips = []
        self.status = 0
        self.log = ""
        self.item_id = None

    def addLogEntry(self, entry):
        self.log = self.log + "\n" + entry

    def toJSON(self):
        import json

        return json.dumps(self.toDict())

    def toDict(self):
        to_dict = {
            "name": self.name,
            "umid": self.umid,
            "timecode": self.timecode,
            "duration": self.duration,
            "metadatas": self.metadatas,
            "input_files": self.input_files,
            "output_file": self.output_file,
            "provider": self.provider.name,
            "spanned": self.spanned,
            "spanned_with": self.spanned_with,
            "spanned_clips": self.spanned_clips,
            "status": self.status,
            "log": self.log,
            "item_id": self.item_id,
        }

        return to_dict