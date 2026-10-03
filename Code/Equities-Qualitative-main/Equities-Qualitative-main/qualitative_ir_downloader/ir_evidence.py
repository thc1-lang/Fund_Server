"""Independent ownership evidence, separate from HTTP/browser availability."""
import re
from urllib.parse import urlsplit
import tldextract
from .company_identity import identify,fold
from .urls import rejected_domain
DOMAIN=tldextract.TLDExtract(suffix_list_urls=(),cache_dir=None)
IR=re.compile(r'investor(?:s|\s+relations)?|\bIR\b',re.I)
NOISE={'amazon.com','openai.com','chatgpt.com','weather.com','timeanddate.com','dictionary.com','hotmail.com','homedepot.com','minecraft.net','imdb.com','ft.com','zacks.com','tipranks.com','fintel.io','investing.com','simplywall.st','stocktitan.net'}

def noise(url):
    d=DOMAIN(url)
    return rejected_domain(url) or d.top_domain_under_public_suffix in NOISE or bool(set(d.subdomain.split('.'))&{'shop','eshop','store','myair','support','forum','forums','community'})

def brand_domain(url,name):
    identity=identify(name);brand=fold(DOMAIN(url).domain)
    aliases=[fold(a) for a in identity.brand_candidates]
    if brand in aliases:return 2
    # Compound brand domains are useful candidates, but are weaker ownership evidence.
    return 1 if any(len(a)>=4 and brand.startswith(a) and brand[len(a):] in {'semi','semiconductor','tech','technology','networks','medicines','holdings'} for a in aliases) else 0

def rank_result(url,name,ticker='',title='',snippet=''):
    if noise(url):return -1000
    identity=identify(name);d=DOMAIN(url);text=title+' '+snippet
    company=identity.legal_match(text);brand=brand_domain(url,name)
    tick=bool(ticker and re.search(r'\b'+re.escape(ticker)+r'\b',text,re.I))
    ir=bool(IR.search(title+' '+urlsplit(url).path)) or d.subdomain in {'ir','investor','investors'}
    # Unfamiliar domains need actual company evidence, not just a generic /investors URL.
    if not brand and (not company or not ir):return -100
    return 25*brand+35*company+15*tick+20*ir+10*bool(IR.search(title))

def ownership(url,name,ticker,title='',body='',search_title='',snippet='',corporate_domain=None):
    identity=identify(name);d=DOMAIN(url);evidence=[];score=0
    if noise(url):return 0,['Rejected unrelated/aggregator domain']
    same=bool(corporate_domain and d.top_domain_under_public_suffix==corporate_domain)
    legal_page=(identity.legal_match(title) or identity.brand_match(title)) and identity.legal_match(body)
    legal_search=identity.legal_match(search_title) and identity.legal_match(snippet)
    ticker_page=bool(ticker and re.search(r'\b'+re.escape(ticker)+r'\b',body,re.I))
    ir_page=bool(IR.search(title)) and bool(re.search(r'financial|sec filings|annual report|stock|earnings',body,re.I))
    branded=brand_domain(url,name)
    if same:score+=55;evidence.append('Same registrable domain as independently verified corporate identity')
    if branded==2:score+=20;evidence.append('Exact company brand in registrable domain')
    elif branded:score+=10;evidence.append('Compound company brand in registrable domain')
    if legal_page:score+=40;evidence.append('Company identity in page title and body')
    if ticker_page:score+=20;evidence.append('Ticker in page content')
    if ir_page:score+=20;evidence.append('IR title and financial/filing content')
    if d.subdomain in {'ir','investor','investors'}:score+=10;evidence.append('Dedicated IR subdomain')
    if identity.legal_match(search_title) and IR.search(search_title):score+=15;evidence.append('Search title identifies company and investor relations')
    if identity.legal_match(snippet):score+=10;evidence.append('Search snippet identifies company')
    # No circular proof: host similarity alone and search results alone cannot verify a mismatched domain.
    strong=(same and (identity.legal_match(search_title) or legal_page)) or (legal_page and ir_page and (ticker_page or branded))
    return min(score,100) if strong else min(score,74),evidence
