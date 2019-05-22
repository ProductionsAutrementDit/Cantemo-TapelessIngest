import os
from lxml import etree
from MAM import ingest_file

root_path = "path_to_media_folder"

if os.path.isdir(root_path):
    xml_file = ""
    # Search an hedge MHL file
    for file in os.listdir(root_path):
        if file.endswith(".mhl"):
            xml_file = file
    
    if xml_file is "":
        print 'No MHL file found'
    
    else:
        # Parse the MHL file
        tree = etree.parse(os.path.join(root_path,xml_file))
        root = tree.getroot()
        
        # Xpath to the infos metadata
        xpath_infos = "/hashlist/hedge/info"
        
        #retrieve the raw info value
        infos_elements = root.xpath(xpath_infos)
        infos_string = infos_elements[0].text
        
        metadatas = {}
        
        # Split raw value with the first delimiter
        infos = infos_string.split('[')
        # Clean empty values
        infos = [x for x in infos if x]
        
        # For eache values, split again to retrieve a key/value pair, then assign it to the metadatas dictionary
        for info in infos:
            key, value = info.split(']', 2)
            metadatas[key] = value
        
        # Xpath to the files hashs
        xpath_files = "/hashlist/hash/file"
        files_elements = root.xpath(xpath_files)
        
        for files_element in files_elements:
            # Ingest file in the MAM
            filepath = os.path.join(root_path, files_element.text)
            ingest_file(filepath, metadatas)

else :
    print 'This is not a valid path'