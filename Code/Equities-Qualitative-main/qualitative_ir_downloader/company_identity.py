"""Company identity for discovery; aliases are search hints, never ownership proof."""
from dataclasses import dataclass,asdict
import re
import unicodedata

SECURITY=re.compile(r'\b(?:sponsored\s+(?:ADR|ADS)|American\s+Depositary\s+(?:Shares|Receipts)|ordinary\s+shares|common\s+stock|class\s+[A-Z](?:\s+(?:shares|stock))?|ADR|ADS)\b',re.I)
LEGAL=re.compile(r'(?:[\s,.-]+)(?:incorporated|inc|corporation|corp|limited|ltd|plc|holdings|holding)\.?$',re.I)

def fold(text):return ''.join(re.findall(r'[a-z0-9]+',unicodedata.normalize('NFKC',text).casefold()))

@dataclass(frozen=True)
class CompanyIdentity:
    raw_company_name:str
    normalized_company_name:str
    brand_candidates:tuple[str,...]
    def record(self):return asdict(self)
    def legal_match(self,text):
        tokens=re.findall(r'[a-z0-9]+',self.normalized_company_name.casefold())
        tokens=[t for t in tokens if t not in {'group','holding','holdings'}]
        # Punctuation variants (Global-e / Globale, Trip.com) are equivalent.
        normalized=fold(text)
        return bool(tokens) and ''.join(tokens) in normalized
    def brand_match(self,text):
        normalized=fold(text)
        return any(len(fold(alias))>=4 and fold(alias) in normalized for alias in self.brand_candidates)

def identify(name):
    normalized=SECURITY.sub('',unicodedata.normalize('NFKC',name))
    normalized=re.sub(r'\s+',' ',normalized).strip(' ,.-')
    while True:
        new=LEGAL.sub('',normalized).strip(' ,.-')
        # A dotted brand such as Trip.com Group retains its display identity.
        if '.' not in new:new=re.sub(r'\s+Group$','',new,flags=re.I).strip()
        if new==normalized:break
        normalized=new
    normalized=normalized or name.strip()
    aliases=[normalized]
    base=re.sub(r'\s+(?:group|online)$','',normalized,flags=re.I)
    aliases.append(base)
    words=base.split()
    if len(words)>1:aliases.append(words[0])
    if '-' in base:aliases.append(base.replace('-',''))
    return CompanyIdentity(name,normalized,tuple(dict.fromkeys(a for a in aliases if len(a)>=3)))

def domain_candidates(name):
    identity=identify(name);out=[]
    for alias in identity.brand_candidates:
        if re.fullmatch(r'[a-z0-9-]+\.(?:com|net|org)',alias,re.I):out.append(alias.lower())
        elif not re.search(r'\.[a-z]{2,}\s',alias,re.I):
            brand=fold(alias)
            if len(brand)>=4:out.append(brand+'.com')
    return list(dict.fromkeys(out))[:3]
