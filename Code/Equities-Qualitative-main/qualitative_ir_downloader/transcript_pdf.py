"""Selectable-text paginated event transcripts with embedded Unicode fonts."""
from datetime import datetime, timezone
from pathlib import Path
from html import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
from reportlab.lib.pagesizes import A4
from .filesystem import atomic_json
from .privacy import safe_record

def timestamp(seconds):
    value=max(0,int(seconds));return f"{value//3600:02}:{value%3600//60:02}:{value%60:02}"

def transcript_record(stock,event,segments):
    record = safe_record({"schema_version":1,"ticker":stock.ticker,"company_name":stock.company_name,"event":{"title":event.title,"date":event.date,"type":event.event_type},"source":{"event_url":event.event_url,"webcast_url":event.webcast_url,"provider":event.provider,"official_transcript_url":event.official_transcript_url,"method":event.method},"transcription":{"engine":event.transcription_engine,"model":event.transcription_model,"language":event.language,"device":event.device,"duration_seconds":event.duration_seconds},"retrieved_at":datetime.now(timezone.utc).isoformat(),"segments":[]})
    record["segments"]=segments
    record["transcription"]["configuration"]=event.transcription_config
    record["transcript_qa"]=event.transcript_qa
    record["timings"]=event.timings
    record["performance"]=event.performance
    record["source"].update(official_captions_url=event.official_captions_url, caption_format=event.caption_format)
    record["transcript_method"]=event.method
    record["generated_by_whisper"]=event.method=="generated_transcript" and event.transcription_engine=="faster-whisper"
    if event.method=="official_captions":
        record["captions"]=event.caption_diagnostics
    return record

def create_pdf(path,record):
    import reportlab
    fonts=Path(reportlab.__file__).parent/"fonts"
    for name,file in [("Transcript","Vera.ttf"),("TranscriptBold","VeraBd.ttf")]:
        if name not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont(name,str(fonts/file)))
    styles=getSampleStyleSheet()
    body=ParagraphStyle("TranscriptBody",fontName="Transcript",fontSize=9.5,leading=14,spaceAfter=9,wordWrap="LTR",splitLongWords=True)
    heading=ParagraphStyle("TranscriptHeading",parent=body,fontName="TranscriptBold",fontSize=17,leading=22,spaceAfter=12)
    sub=ParagraphStyle("TranscriptSub",parent=body,fontName="TranscriptBold",fontSize=11,leading=15)
    small=ParagraphStyle("TranscriptMeta",parent=body,fontSize=8,leading=12,textColor=colors.HexColor("#44515D"))
    stamp=ParagraphStyle("TranscriptStamp",parent=small,keepWithNext=True,spaceAfter=3)
    story=[Paragraph(escape(record["ticker"]+" - "+record["company_name"]),sub),Paragraph(escape(record["event"]["title"]),heading)]
    e=record["event"];source=record["source"];t=record["transcription"]
    fields=[("Date",e["date"] or "Unknown"),("Event type",e["type"]),("Event URL",source["event_url"]),("Webcast provider",source["provider"] or "generic"),("Transcript source","Official company/provider transcript" if source["method"]=="official_transcript" else "Official webcast captions" if source["method"]=="official_captions" else "Automatically generated from publicly accessible webcast audio"),("Method",source["method"]),("Model",t["model"] or "Not applicable"),("Duration",timestamp(t["duration_seconds"]) if t["duration_seconds"] else "Unknown"),("Retrieved (UTC)",datetime.fromisoformat(record["retrieved_at"]).strftime("%Y-%m-%d %H:%M"))]
    if source["method"]=="official_captions":
        fields.extend([("Caption format",source.get("caption_format") or "Unknown"),
                       ("Caption source",source.get("official_captions_url") or "Unknown"),
                       ("Caption end",timestamp(record.get("captions",{}).get("last_timestamp",0)))])
    for label,value in fields:story.append(Paragraph(escape(f"{label}: {value}"),small))
    if record.get('transcript_qa'):
        qa=record['transcript_qa']
        story.append(Paragraph(escape('Transcript QA: '+qa['status']+'; '+', '.join(qa.get('warnings',[]))),small))
    story.extend([Spacer(1,12),Paragraph("TRANSCRIPT",sub)])
    for segment in record["segments"]:
        marker=f"[{timestamp(segment['start'])}]" if segment.get("start") is not None else ""
        if segment.get("speaker"):marker+=" "+segment["speaker"]
        if marker:story.append(Paragraph(escape(marker),stamp))
        story.append(Paragraph(escape(segment["text"]).replace("\n","<br/>"),body))
    def footer(canvas,doc):
        canvas.setFont("Transcript",8);canvas.setFillColor(colors.HexColor("#44515D"))
        canvas.drawString(42,25,record["ticker"]+" | Event transcript")
        canvas.drawRightString(A4[0]-42,25,f"Page {doc.page}")
    temporary=Path(path).with_suffix(".pdf.part")
    SimpleDocTemplate(str(temporary),pagesize=A4,leftMargin=42,rightMargin=42,topMargin=40,bottomMargin=43,title=e["title"],author="Qualitative IR Downloader").build(story,onFirstPage=footer,onLaterPages=footer)
    from .pdf_utils import validate_pdf
    validate_pdf(temporary.read_bytes());temporary.replace(path)
