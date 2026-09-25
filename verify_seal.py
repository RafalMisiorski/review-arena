"""Verify that key.json reproduces key.sealed (sha-256 over salt + canonical key). Needs: pip install sealeval"""
import json
from sealeval.sealing.keyseal import verify_seal
key = json.load(open("key.json", encoding="utf-8"))
sealed = json.load(open("key.sealed", encoding="utf-8"))
print(verify_seal(key, sealed))
