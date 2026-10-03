"""Public contact registration only; no login, CAPTCHA or email bypass."""
import re
import logging
from urllib.parse import urljoin, urlsplit, unquote_plus, urlunsplit, parse_qsl, urlencode
from .privacy import redact_url
import tldextract
from .models import CollectionError
log=logging.getLogger(__name__)
DOMAIN=tldextract.TLDExtract(suffix_list_urls=(),cache_dir=None)

def registrable_domain(url):
    host=urlsplit(url if "://" in url else "https://"+url).hostname or ""
    ext=DOMAIN(host)
    return ext.top_domain_under_public_suffix or host.casefold()

def approved_destination(url, approved):
    return urlsplit(url).scheme=="https" and registrable_domain(url) in {d.casefold() for d in approved}

def registration_url(url):
    p=urlsplit(redact_url(url))
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode([(k,"REDACTED") for k,v in parse_qsl(p.query)]),""))

async def registration_response(route, **kwargs):
    return await route.fetch(max_redirects=0,timeout=30000,**kwargs)

async def protect_submission(context, profile, approved, event):
    # Ordinary browser routing does not reliably intercept redirect hops.
    # Resolve PII-bearing requests one hop at a time before exposing a redirect.
    values=[str(getattr(profile,k)).casefold() for k in ("name","first_name","last_name","email","company") if getattr(profile,k,"")]
    def contains_values(value):
        for _ in range(3):value=unquote_plus(value)
        return any(v in value.casefold() for v in values)
    def stopped(url):
        event.registration_status="REGISTRATION_APPROVAL_REQUIRED"
        event.errors.append({"code":"REGISTRATION_APPROVAL_REQUIRED","stage":"submission_destination","domain":registrable_domain(url)})
    async def guard(route):
        request=route.request
        sensitive=contains_values(request.url+" "+(request.post_data or ""))
        foreign=not approved_destination(request.url,approved)
        if foreign and (sensitive or bool(request.post_data)):
            if sensitive:stopped(request.url)
            await route.abort();return
        if not sensitive:
            await route.fallback();return
        current=request.url;method=request.method;body=request.post_data
        headers=await request.all_headers()
        try:
            for hop in range(8):
                if not approved_destination(current,approved):
                    stopped(current);await route.abort();return
                response=await registration_response(route,url=current,method=method,post_data=body or "",headers=headers)
                event.registration_redirect_chain.append(registration_url(current))
                if response.status in {301,302,303,307,308} and response.headers.get("location"):
                    target=urljoin(current,response.headers["location"])
                    if urlsplit(target).hostname!=urlsplit(current).hostname:
                        headers={k:v for k,v in headers.items() if k.lower() not in {"cookie","authorization","host","referer"}}
                    if response.status==303 or response.status in {301,302} and method=="POST":
                        method="GET";body=None
                        headers={k:v for k,v in headers.items() if k.lower() not in {"content-type","content-length"}}
                    current=target
                    continue
                if response.status in {401,403,429}:
                    event.registration_status="REGISTRATION_FAILED"
                    event.errors.append({"code":"REGISTRATION_FAILED","stage":"registration_response","http_status":response.status})
                if hop and request.is_navigation_request() and not contains_values(current):
                    # A fresh GET restores the real page base URL without forwarding PII.
                    await route.fulfill(status=303,headers={"location":current,"referrer-policy":"no-referrer"},body="")
                else:
                    await route.fulfill(status=response.status,headers=response.headers,body=await response.body())
                return
            event.registration_status="REGISTRATION_FAILED"
            await route.abort()
        except Exception:
            event.registration_status="REGISTRATION_FAILED"
            await route.abort()
    await context.route("**/*",guard)


async def discover_destination(page,event):
    """Inspect visible forms only; never fill controls, consent, or submit."""
    for frame in page.frames:
        forms=frame.locator("form")
        for i in range(await forms.count()):
            form=forms.nth(i)
            if not await form.is_visible(): continue
            fields=await form.locator("input,select,textarea").evaluate_all("els=>els.map(e=>({type:e.type,label:[...(e.labels||[])].map(x=>x.innerText).join(' ')||e.getAttribute('aria-label')||e.placeholder||e.name||e.id,visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))")
            visible=[field_meaning(x['label']) or x['type'] for x in fields if x['visible'] and x['type'] not in {'hidden','submit','button'}]
            if 'email' not in visible or not set(visible).intersection({'name','first_name','last_name','company'}): continue
            action=urljoin(frame.url,await form.get_attribute('action') or frame.url)
            host=urlsplit(action).hostname or urlsplit(frame.url).hostname or ''
            domain=registrable_domain(action)
            event.registration_required=True; event.registration_page_url=frame.url; event.registration_form_action=action; event.registration_destination_domain=domain; event.registration_fields=sorted(set(visible))
            event.registration_status='DESTINATION_DISCOVERED'; event.transition('REGISTRATION_DESTINATION_DISCOVERED')
            return {"page_url":frame.url,"form_action":action,"domain":domain,"fields":event.registration_fields}
    return None

def field_meaning(label):
    text=re.sub(r"[_-]+"," ",label).lower()
    if re.search(r"first|given|forename",text):return "first_name"
    if re.search(r"last|surname|family",text):return "last_name"
    if re.search(r"e.?mail",text):return "email"
    if re.search(r"company|organi[sz]ation|employer|firm",text):return "company"
    if re.search(r"occupation|job|position|role|title|profession",text):return "occupation"
    if re.search(r"country",text):return "country"
    if re.search(r"name",text):return "name"
    return None

def access_state(text):
    if re.search(r"verify.{0,30}email|check your (?:email|inbox)|link.{0,30}(?:sent|emailed)|confirmation email",text,re.I):return "EMAIL_VERIFICATION_REQUIRED"
    if re.search(r"sign in with|single sign.on|employee.only|subscription required|paywall|payment required|enter.{0,20}password|verification code|multi.factor",text,re.I):return "AUTH_REQUIRED"
    if re.search(r"complete.{0,20}captcha|verify (?:that )?you are human",text,re.I):return "CAPTCHA_REQUIRED"
    return None

async def register(page, profile, event, submitted):
    for frame in page.frames:
        body=await frame.locator("body").inner_text()
        state=access_state(body[:12000])
        if state:raise CollectionError(state,"Webcast requires an additional access step")
        if await frame.locator('input[type="password"]:visible').count():raise CollectionError("AUTH_REQUIRED","Password authentication required")
        if await frame.locator('iframe[src*="recaptcha"]:visible, iframe[src*="hcaptcha"]:visible, .g-recaptcha:visible, .h-captcha:visible').count():raise CollectionError("CAPTCHA_REQUIRED","Visible CAPTCHA prevents registration")
        forms=frame.locator("form")
        for i in range(await forms.count()):
            form=forms.nth(i)
            if not await form.is_visible():continue
            fields=await form.locator("input,select,textarea").evaluate_all("""els=>els.map((e,i)=>({index:i,type:e.type,required:e.required||e.getAttribute('aria-required')==='true',label:[...[...e.labels||[]].map(x=>x.innerText),e.getAttribute('aria-label'),e.placeholder,e.name,e.id].filter(Boolean).join(' '),visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))""")
            meanings={field_meaning(x["label"]) for x in fields if x["visible"]}
            text=await form.inner_text()
            if "email" not in meanings or not meanings.intersection({"name","first_name","last_name","company"}) or re.search(r"newsletter|subscribe to|email alerts",text,re.I):continue
            event.registration_required=True;event.registration_status="REQUIRED";event.transition("REGISTRATION_REQUIRED")
            key=frame.url+"|"+str(i)
            if key in submitted:raise CollectionError("REGISTRATION_REQUIRED","Registration did not complete; submission loop prevented")
            action=urljoin(frame.url,await form.get_attribute("action") or frame.url)
            host=(urlsplit(action).hostname or "").casefold()
            destination=registrable_domain(action)
            approved={d.casefold().lstrip(".") for d in getattr(page.context,"_approved_registration_domains",())}
            event.registration_page_url=frame.url; event.registration_form_action=action; event.registration_destination_domain=destination
            event.registration_fields=sorted(m for m in meanings if m)
            log.info("Registration provider=%s page=%s destination=%s fields=%s",event.provider,redact_url(frame.url),destination,event.registration_fields)
            if not approved_destination(action,approved) or not approved_destination(frame.url,approved):
                event.registration_page_url=frame.url; event.registration_form_action=action; event.registration_destination_domain=destination
                raise CollectionError("REGISTRATION_APPROVAL_REQUIRED",f"Registration destination not approved: {destination}")
            await protect_submission(page.context,profile,approved,event)
            for f in fields:
                if not f["visible"] or f["type"] in {"hidden","submit","button","reset"}:continue
                control=form.locator("input,select,textarea").nth(f["index"])
                label=f["label"]
                if f["type"] in {"checkbox","radio"}:
                    marketing=bool(re.search(r"market|subscribe|communications|contact me|products|promotional",label,re.I))
                    required_terms=f["required"] and bool(re.search(r"terms|privacy|accept",label,re.I))
                    if marketing:
                        if f["type"]=="checkbox":await control.set_checked(False)
                        elif await control.is_checked():raise CollectionError("REGISTRATION_REQUIRED","Preselected marketing radio cannot be submitted")
                        if f["required"]:raise CollectionError("REGISTRATION_REQUIRED","Mandatory marketing consent was not accepted")
                    elif required_terms:
                        await control.set_checked(True);event.registration_acceptances.append("Required webcast terms/privacy acceptance")
                        log.info("Accepted required webcast terms/privacy checkbox")
                    elif f["type"]=="checkbox":await control.set_checked(False)
                    elif f["required"]:raise CollectionError("REGISTRATION_REQUIRED","Unsupported required radio field")
                    continue
                meaning=field_meaning(label)
                if not meaning:
                    if f["required"]:raise CollectionError("REGISTRATION_REQUIRED","Unmapped mandatory registration field")
                    continue
                value=getattr(profile,meaning,"")
                if f["type"]=="select-one":
                    options=await control.locator("option").evaluate_all("els=>els.map(e=>({value:e.value,label:e.textContent.trim(),disabled:e.disabled}))")
                    choices=("Other","Individual investor","Investor","Analyst","Finance","Professional") if meaning=="occupation" else (value,)
                    chosen=next((o for choice in choices for o in options if choice and o["label"].casefold()==choice.casefold() and not o["disabled"]),None)
                    if chosen:await control.select_option(chosen["value"])
                    elif f["required"]:raise CollectionError("REGISTRATION_REQUIRED","Required selection has no configured value")
                elif value:await control.fill(value)
                elif f["required"]:raise CollectionError("REGISTRATION_REQUIRED","Required registration value not configured")
            submit=form.get_by_role("button",name=re.compile(r"^(register|submit|continue|watch now|access webcast|launch webcast|listen now)$",re.I))
            if not await submit.count():submit=form.locator('input[type="submit"]')
            if not await submit.count():raise CollectionError("REGISTRATION_REQUIRED","Registration submit control unavailable")
            submitted.add(key);event.submission_attempted=True;log.info("Registration submission attempted: provider=%s destination=%s",event.provider,destination)
            await submit.first.click()
            await page.wait_for_timeout(2500)
            event.post_submit_url=registration_url(page.url)
            if event.registration_status in {"REGISTRATION_APPROVAL_REQUIRED","REGISTRATION_FAILED"}:
                raise CollectionError(event.registration_status,"Registration request stopped; see destination/stage diagnostics")
            state=access_state((await page.locator("body").inner_text())[:12000])
            if state:raise CollectionError(state,"Registration requires a follow-up access step")
            event.registration_status="SUBMITTED"
            log.info("Registration submitted; post-submit URL=%s",event.post_submit_url)
            return True
    return False
