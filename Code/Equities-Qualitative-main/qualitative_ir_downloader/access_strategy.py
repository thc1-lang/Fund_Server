"""Operational route hints scoped to an already verified IR identity."""
import json
from .filesystem import atomic_json
from .scoring import company_key
from .urls import canonicalize_url

def key(stock,root):return stock.ticker.upper()+'|'+company_key(stock.company_name)+'|'+canonicalize_url(root)

def load(directory,stock,root):
    try:
        data=json.loads((directory/'ir_access_strategy.json').read_text(encoding='utf-8'))
        if key(stock,root) in data:return data[key(stock,root)]
        for identity,item in data.items():
            parts=identity.split('|',2)
            if len(parts)==3 and parts[0].upper()==stock.ticker.upper() and company_key(parts[1])==company_key(stock.company_name) and canonicalize_url(parts[2])==canonicalize_url(root):return item
        return {}
    except (OSError,ValueError):return {}


def save(directory,stock,root,record):
    path=directory/'ir_access_strategy.json'
    try:data=json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:data={}
    data[key(stock,root)]=record
    atomic_json(path,data)
