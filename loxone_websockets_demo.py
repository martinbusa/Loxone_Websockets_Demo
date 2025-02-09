#Loxone Websocket Demo (developed on Python 3.7.7, Thonny)
#This is a demo program to establish a websocket connection to the loxone miniserver
#Referencing https://www.loxone.com/dede/wp-content/uploads/sites/2/2020/05/1100_Communicating-with-the-Miniserver.pdf
#This is a quite crappy documentation
#Here's the summary for a Miniserver Ver.1
#Due to security requirements, the communication between Miniserver and client needs to be encrypted
#In order to allow random clients to connect, a fixed shared secret cannot be used. However, as en encryption
#mechanism AES was chosen, which is a symmetric cryptographic method meaning the keys are the same on receiving
#and sending end. To overcome this, the client will define which AES key/iv to use and let the Miniserver know.
#To do so, the Miniserver provides its public RSA key to allow an assymetric encryption to be used for sending
#the AES key/iv pair. RSA limits the size of the payload - that's why it is not an option to only use RSA
#Furthermore, to authenticate, nowadays a token is used instead of user/password for each request.
#So, generally you could say we are:
# 1) Defining the AES Key and IV on the client side (in this program)
# 2) Retrieving the RSA public key and encrypting the AES Key with it
# 3) Send the AES Key/IV to the Miniserver in a key exchange
# 4) Request an authentication token (as we assume that we don't have one yet)
# 4a) Hash the User and Password to pass to the Miniserver to get the token
# 4b) Encrypt the Command using the AES Key and IV
# 5) wait for something to happen (maybe you now press some key in your home...)

#Imports
import requests   #lib for GET, POST, PUT etc.
import websockets #lib for websockets
import asyncio    #Asynchronous operation is necessary to deal with websockets

#Install pyCryptoDome NOT pyCrypto
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES
from Crypto import Random
from Crypto.Cipher import PKCS1_v1_5

import base64    #necessary to encode in Base64
import secrets   #helpful to produce hashes and random bytes
import binascii  #hexlify/unhexlify allows to get HEX-Strings out of bytes-variables
import json      #working with JSON
import hashlib   #Hashing
import hmac      #Key-Hashing
import urllib    #necessary to encode URI-compliant
from settings import Env  #your settings.py
from bitstring import ConstBitStream #install bistring --> necessary to deal with Bit-Messages
from nested_lookup import nested_lookup # install nested-lookup --> great for the dict with all UUIDs

#Some Configuration/Definition --> Edit as needed

#Fixed values (for demo purpose only) - should be replaced by randomly generated (page 7, step 4 & 5)
aes_key = str("6A586E3272357538782F413F4428472B4B6250655368566B5970337336763979")
aes_iv = str("782F413F442A472D4B6150645367566B")

# Configuration 

#Either you have a .env-File with the following settings OR Fill your own values here in the script
#LOX_USER = "user1"
#LOX_PASSWORD = "passwordxyz"
#LOX_IP = "192.168.1.1"
#LOX_PORT = "80"
env = Env("LOX_")
myConfig = {
    'user': '',
    'password' : '',
    'ip' : '',
    'port' : ''
    }
env.setDefaults(myConfig)

myUser = env.user
myPassword = env.password
myIP = env.ip
myPort = env.port

myUUID = "093302e1-02b4-603c-ffa4ege000d80cfd" #A UUID of your choosing --> you can use the one supplied as well
myIdentifier = "lox_test_script" #an identifier of your chosing
myPermission = 2 #2 for short period, 4 for long period

rsa_pub_key = None #possibility to set the key for debugging, e.g. "-----BEGIN PUBLIC KEY-----\nMxxxvddfDCBiQKBgQCvuJAG7r0FdysdfsdfBl/dDbxyu1h0KQdsf7cmm7mhnNPCevRVjRB+nlK5lljt1yMqJtoQszZqCuqP8ZKKOL1gsp7F0E+xgZjOpsNRcLxglGImS6ii0oTiyDgAlS78+mZrYwvow3d05eQlhz6PzqhAh9ZHQIDAQAB\n-----END PUBLIC KEY-----"

### Classes used ###

#Description of a Loxone Header message (page 14)
class LoxHeader:
#    typedef struct {
#      BYTE cBinType; // fix 0x03 --> fixed marking a header, raise exception if not there
#      BYTE cIdentifier; // 8-Bit Unsigned Integer (little endian) --> Kind of message following the header
#            0: Text  1: Binary  2,3,4: Event Tables  5: out-of-service   6: Keep-alive  7: Wheather
#      BYTE cInfo; // Info
#      BYTE cReserved; // reserved
#      UINT nLen; // 32-Bit Unsigned Integer (little endian)
    msg_type = None
    exact2Follow = False

    def __init__(self, header_msg: bytes):
        #First byte
        if header_msg[0:1] != bytes.fromhex('03'):
            raise ValueError("This is not a header message")
        self.msg_type = self.__setIdentifier(header_msg[1:2])
        self.exact2Follow = self.__setExact2Follow(header_msg[2:3])
        #Bytes 4-8 could be decoded some other time --> would allow prediction of load times
        
        
    def __setIdentifier(self, secondByte: bytes):
        switch_dict = {
            b'\x00': 'text', #"Text-Message"
            b'\x01': 'bin', #"Binary File"
            b'\x02': 'value', #"Event-Table of Value-States
            b'\x03': 'text_event', # Event-Table of Text-States
            b'\x04': 'daytimer', #Event-Table of Daytimer-States
            b'\x05': 'out-of-service', #e.g. Firmware-Upgrade - no following message at all. Connection closes
            b'\x06': 'still_alive', #response to keepalive-message
            b'\x07': 'weather' # Event-Table of Wheather-States
            }
        return switch_dict.get(secondByte, "invalid")
    
    def __setExact2Follow(self, thirdByte: bytes):
        bitstream = ConstBitStream(thirdByte)
        bitstream.pos = 0
        if bitstream.read('bin:1') == '1':
            return True
        else:
            return False
            
# Base Class for State Messages
class LoxState:
    
    @staticmethod
    def decodeUUID(uuid: bytes) -> str:

        #Decode UUID
        bitstream = ConstBitStream(uuid)
        data1, data2, data3, data41, data42, data43, data44, data45, data46, data47, data48 = bitstream.unpack('uintle:32, uintle:16, uintle:16, uint:8, uint:8, uint:8, uint:8, uint:8, uint:8, uint:8, uint:8')
        uuid = "{:08x}-{:04x}-{:04x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}".format(data1, data2, data3, data41, data42, data43, data44, data45, data46, data47, data48)
        return uuid
   
    def setUUID(self, uuid: bytes):
        self.uuid  = "" #UUID as String

        self.uuid = LoxState.decodeUUID(uuid)
   
class LoxValueState(LoxState):
    #Consists of UUID (16 byte) and Value 64-Bit Float (little endian) value
    # Each State-Entry in the table is consequently 24 byte long
    #
    #     Binary-Structure of a UUID
    # typedef struct _UUID {
    #  unsigned long Data1; // 32-Bit Unsigned Integer (little endian)
    #  unsigned short Data2; // 16-Bit Unsigned Integer (little endian)
    #  unsigned short Data3; // 16-Bit Unsigned Integer (little endian)
    #  unsigned char Data4[8]; // 8-Bit Uint8Array [8] (little endian)
    #Example from the structure-file in json however:
    # UUID: "12c3abc1-024e-1135-ffff7ba5fa36c093","name":"Gästezimmer UG Süd"
    # The strings are the hex representations of the numbers
    
    def __init__(self, valueStateMsg: bytes):
        #for BitStream: https://bitstring.readthedocs.io/en/latest/constbitstream.html?highlight=read#bitstring.ConstBitStream.read
        
        self.value = 0  #Value as Float
        
        LoxState.__init__(self)
        
        self.setUUID(valueStateMsg[0:16])
        
        #Decode Value
        bitstream = ConstBitStream(valueStateMsg[16:24])
        value_list = bitstream.unpack('floatle:64')
        self.value = "{:g}".format(value_list[0])
        
           
        
    @classmethod #cls is a "keyword" for the class itself. Hence, parseTable is not bound to an instance  
    def parseTable(cls, eventTable: bytes) -> dict:
        # take a longer message and split it and create ValueState-Instances
        # Return a dict with UUIDs and values
        instances = list()

        for i in range(0, len(eventTable), 24):
            instances.append( cls(eventTable[i:i+24]) ) # cls() creates an instance of the class itself
            
        values = dict()
        for inst in instances:
            values[inst.uuid] = inst.value
            
        return values
    
# Text State - derived from LoxState
class LoxTextState(LoxState):
    #instance variables: uuid, uuid_icon, text
    #page 17/16 in Loxone Guide
    #typedef​ ​struct​ { ​// starts at multiple of 4
    #   PUUID​ ​uuid​; // 128-Bit uuid
    #   PUUID​ ​uuidIcon​; // 128-Bit uuid of icon
    # ​  unsigned long ​textLength;  // 32-Bit Unsigned Integer (little endian)
    #      // text follows here
    # } ​PACKED​ ​EvDataText​;
    
    def __init__(self, message):
        LoxState.__init__(self)
        
        # extract UUID
        self.setUUID(message[0:16])
        
        # extract icon UUID
        self.uuid_icon = LoxState.decodeUUID(message[16:32])
        
        # calculate Text length
        bitstream = ConstBitStream(message[32:36])
        self.textLen = bitstream.unpack('uintle:32')[0]
        
        # extract Text
        self.text = message[36:36+self.textLen].decode('utf8','strict')

    @classmethod #cls is a "keyword" for the class itself. Hence, parseTable is not bound to an instance  
    def parseTable(cls, eventTable: bytes) -> dict:
        # take a longer message and split it and create ValueState-Instances
        # Return a dict with UUIDs and values
        instances = list()
        
        i = 0
        while i < len(eventTable):
            startI = i
            i+=32 #Skip UUID and icon UUID
            bitstream = ConstBitStream(eventTable[i:i+4])
            textLen = bitstream.unpack('uintle:32')[0]
            i += 4 + textLen #Skip textLen and text itself
            if (i % 4) != 0:
              i += 4 - i % 4 #Skip padding
            instances.append( cls(eventTable[startI:i]) ) # cls() creates an instance of the class itself
            
        values = dict()
        for inst in instances:
            values[inst.uuid] = inst.text
            
        return values
   

### These are the functions used ###
### sync functions ###
# Get the RSA public key from the miniserver and format it so that it is compliant with a .PEM file 
def prepareRsaKey():
    response = requests.get("http://{}:{}/jdev/sys/getPublicKey".format(myIP, myPort))
    rsa_key_malformed = response.json()["LL"]["value"]
    rsa_key_malformed = rsa_key_malformed.replace("-----BEGIN CERTIFICATE-----", "-----BEGIN PUBLIC KEY-----\n")
    rsa_key_wellformed = rsa_key_malformed.replace("-----END CERTIFICATE-----", "\n-----END PUBLIC KEY-----")
    print("RSA Public Key: ", rsa_key_wellformed)
    return rsa_key_wellformed


#Async Functions

#Websocket connection to Loxone
async def webSocketLx():
    
    #Encrypt the AES Key and IV with RSA (page 7, step 6)
    sessionkey = await create_sessionkey(aes_key, aes_iv)
    print("Session key: ", sessionkey) 
    
    #start websocket connection (page 7, step 3 - protocol does not need to be specified apparently)
    async with websockets.connect("ws://{}:{}/ws/rfc6455".format(myIP, myPort)) as myWs:
        
        #Send Session Key (page 8, step 7)
        await myWs.send("jdev/sys/keyexchange/{}".format(sessionkey))
        await myWs.recv()
        response = await myWs.recv()
        sessionkey_answer = json.loads(response)["LL"]["value"]
        
        #Now a ramdom salt of 2 bytes is added (page 8, step 8)
        aes_salt = binascii.hexlify(secrets.token_bytes(2)).decode()
        
        #Now prepare the token collection command with command encryption
        #Objective is to: Request a JSON Web Token “jdev/sys/getjwt/{hash}/{user}/{permission}/{uuid}/{info}”
        #--> This request must be encrypted
        # page 8, step 9b
        
        #Sending encrypted commands over the websocket (page 27, step 1)
        # Get the JSON web token (page 22, 23)
        getTokenCommand = "salt/{}/jdev/sys/getjwt/{}/{}/{}/{}/{}".format(aes_salt, await hashUserPw(myUser, myPassword), myUser, myPermission, myUUID, myIdentifier)
        print("Get Token Command to be encrypted: ", getTokenCommand)
        
        #Now encrypt the command with AES (page 21 step 1 & 2)
        encrypted_command = await aes_enc(getTokenCommand, aes_key, aes_iv)
        message_to_ws = "jdev/sys/enc/{}".format(encrypted_command) # page 21, step 3
        print("Message to be sent: ", message_to_ws)
        
        #Send message to get a JSON webtoken
        await myWs.send(message_to_ws)
        await myWs.recv()
        print("Answer to the Token-Command: ", await myWs.recv()) #And if you get back a 200 the connection is established
        
        #Get the structure file from the Miniserver (page 18)
        await myWs.send("data/LoxAPP3.json")
        header = LoxHeader(await myWs.recv())
        print(header.msg_type)
        print(await myWs.recv())
        structure_file = await myWs.recv()
        struct_dict = json.loads(structure_file)
        print("Structure File: ", json.dumps(structure_file))
        
        await myWs.send("jdev/sps/enablebinstatusupdate")
        
        for i in range(0, 15):
            header = LoxHeader(await myWs.recv())
            message = await myWs.recv()
            if header.msg_type == 'text':
                print("Text message: ", message)
            elif header.msg_type == 'bin':
                print("Binary message: ", message)
            elif header.msg_type == 'value':
                statesDict = LoxValueState.parseTable(message)
                #print(statesDict)
                for uuid in statesDict:
                  nameLookup = nested_lookup(uuid, struct_dict, with_keys = True)
                  name = 'Unknown'
                  if uuid in nameLookup:
                    name = nameLookup[uuid][0]['name']
                  print("Value {}({}): {}".format(name,uuid, statesDict[uuid]))
            elif header.msg_type == 'text_event':
                print("Text message: ", message)
                textsDict = LoxTextState.parseTable(message)
                print(textsDict)
                for uuid in textsDict:
                  nameLookup = nested_lookup(uuid, struct_dict, with_keys = True)
                  name = 'Unknown'
                  if uuid in nameLookup:
                    name = nameLookup[uuid][0]['name']
                  print("Text {}({}): {}".format(name,uuid, textsDict[uuid]))
            elif header.msg_type == 'daytimer':
                print("Daytimer message: ", message)
            elif header.msg_type == 'out-of-service':
                print("Out-of-service message: ", message)
            elif header.msg_type == 'still_alive':
                print("Still alive message: ", message)
            elif header.msg_type == 'still_alive':
                print("Weather message: ", message)
            else:
                print("Unknown message: ", message)
        
# Function to RSA encrypt the AES key and iv
async def create_sessionkey(aes_key, aes_iv):
    payload = aes_key + ":" + aes_iv
    payload_bytes = payload.encode()
    #RSA Encrypt the String containing the AES Key and IV
    #https://8gwifi.org/rsafunctions.jsp
    #RSA/ECB/PKCS1Padding
    pub_key = RSA.importKey(rsa_pub_key)
    encryptor = PKCS1_v1_5.new(pub_key)
    sessionkey = encryptor.encrypt(payload_bytes)
    #https://www.base64encode.org/ to compare
    return base64.standard_b64encode(sessionkey).decode()
    
    
# AES encrypt with the shared AES Key and IV    
async def aes_enc(text, aes_key, aes_iv):
    key = binascii.unhexlify(aes_key)
    iv = binascii.unhexlify(aes_iv)
    print("Key: ", key, "IV: ", iv)
    encoder = AES.new(key, AES.MODE_CBC, iv=iv)
    encrypted_msg = encoder.encrypt(await pad(text.encode()))
    b64encoded = base64.standard_b64encode(encrypted_msg)
    return urllib.parse.quote(b64encoded, safe="") #Return url-Encrypted
 

# ZeroBytePadding to AES block size (16 byte) to allow encryption 
async def pad(byte_msg):
    return byte_msg + b"\0" * (AES.block_size - len(byte_msg) % AES.block_size) #ZeroBytePadding / Zero Padding


# Key-Hash the User and Password HMAC-SHA1 (page 22)
async def hashUserPw(user, password):
    # Get the key to be used for the HMAC-Hashing and the Salt to be used for the SHA1 hashing
    response = requests.get("http://{}:{}/jdev/sys/getkey2/{}".format(myIP, myPort, user))
    print(response.text)
    userKey = response.json()["LL"]["value"]["key"]
    userSalt = response.json()["LL"]["value"]["salt"]
    pwHash = await hash_Password(password, userSalt)
    print("PW Hash: ", pwHash)
    userHash = await digest_hmac_sha1("{}:{}".format(user, pwHash), userKey)
    #The userHash shall be left like it is
    return userHash
    

# Hash the Password plain and simple: SHA1 (page 22)
async def hash_Password(password, userSalt):
    #check if result is this: https://passwordsgenerator.net/sha1-hash-generator/
    tobehashed = password + ":" + userSalt
    print("To be hashed: ", tobehashed)
    hash = hashlib.sha1(tobehashed.encode())
    #according to the Loxone Doc, the password Hash shall be upper case
    hashstring = hash.hexdigest()
    print("Hashed: ", hashstring.upper())
    return hashstring.upper()
    

# HMAC-SHA1 hash something with a given key
async def digest_hmac_sha1(message, key):
    #https://gist.github.com/heskyji/5167567b64cb92a910a3
    #compare: https://www.liavaag.org/English/SHA-Generator/HMAC/  -- key type: text, output: hex
    print("hmac sha1 input: ", message)
    hex_key = binascii.unhexlify(key)
    print("Hex Key: ", hex_key)
    message = bytes(message, 'UTF-8')
    
    digester = hmac.new(hex_key, message, hashlib.sha1)
    signature1 = digester.digest()
    
    signature2 = binascii.hexlify(signature1)    
    print("hmac-sha1 output: ", signature2.decode())
    #return a hex string
    return signature2.decode()
    
    
### THIS is the actual program that executes - type: python loxone_websockets_demo.py ###
rsa_pub_key = prepareRsaKey() #Retrieve the public RSA key of the miniserver (page 7, step 2)
asyncio.get_event_loop().run_until_complete(webSocketLx()) #Start the eventloop (async) with the function webSocketLx


    

