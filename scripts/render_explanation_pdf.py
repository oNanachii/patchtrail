"""Render the project explanation; requires ReportLab and embeddable CJK fonts."""
import argparse
from html import escape
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Flowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
INK = colors.HexColor('#172B40')
BLUE = colors.HexColor('#225C87')
MUTED = colors.HexColor('#627487')
PALE = colors.HexColor('#EFF5FA')
LINE = colors.HexColor('#D8E3EC')
WIDTH = A4[0] - 88


class Diagram(Flowable):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.width = WIDTH
        self.height = 156 if kind == 'architecture' else 78

    def draw(self):
        c = self.canv

        def box(x, y, w, h, title, subtitle=''):
            c.setFillColor(PALE)
            c.setStrokeColor(LINE)
            c.roundRect(x, y, w, h, 7, stroke=1, fill=1)
            c.setFillColor(BLUE)
            c.setFont('CJK-Bold', 11)
            c.drawCentredString(x+w/2, y+h/2+(3 if subtitle else -4), title)
            if subtitle:
                c.setFont('CJK', 8.2)
                c.setFillColor(MUTED)
                c.drawCentredString(x+w/2, y+h/2-12, subtitle)

        def arrow(x1,y1,x2,y2):
            import math
            c.setStrokeColor(BLUE)
            c.setLineWidth(1)
            c.line(x1,y1,x2,y2)
            angle=math.atan2(y2-y1,x2-x1)
            for sign in [-1,1]:
                a=angle+sign*0.5
                c.line(x2,y2,x2-5*math.cos(a),y2-5*math.sin(a))

        if self.kind == 'architecture':
            box(0,96,118,48,'浏览器','表单、审阅与确认')
            box(153,96,171,48,'FastAPI后端','权限、预算与任务编排')
            box(359,96,WIDTH-359,48,'SQLite','状态与事件')
            box(153,9,171,48,'DeepSeek','请求工具与提出补丁')
            box(359,9,WIDTH-359,48,'受限容器','固定测试与结果')
            arrow(120,120,149,120)
            arrow(326,120,355,120)
            arrow(238,92,238,61)
            arrow(300,92,395,61)
        else:
            values = ([('任务快照','固定源码和测试'),('调查与提案','模型请求受控工具'),
                       ('明确确认','审阅具体修改'),('容器验证','新副本测试与报告')]
                      if self.kind == 'workflow' else
                      [('排队','queued'),('调查','diagnosing'),('待确认','awaiting_approval'),
                       ('验证','verifying'),('成功 / 失败','终态记录')])
            n=len(values)
            gap=12
            w=(WIDTH-gap*(n-1))/n
            for i,(title,subtitle) in enumerate(values):
                x=i*(w+gap)
                box(x,12,w,54,title,subtitle)
                if i<n-1:
                    arrow(x+w+2,39,x+w+gap-2,39)


class NumberedCanvas(canvas.Canvas):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._pages=[]

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total=len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setStrokeColor(LINE)
            self.line(44,43,A4[0]-44,43)
            self.setFillColor(MUTED)
            self.setFont('CJK',8)
            self.drawString(44,28,'PatchTrail · 使用、架构与面试讲解 · v0.3.1')
            self.drawRightString(A4[0]-44,28,f'{self._pageNumber:02d} / {total:02d}')
            super().showPage()
        super().save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-regular',default='/System/Library/Fonts/STHeiti Light.ttc')
    parser.add_argument('--font-bold',default='/System/Library/Fonts/STHeiti Medium.ttc')
    parser.add_argument('--output',type=Path,default=ROOT/'output/pdf/PatchTrail_项目讲解_2026-10-29.pdf')
    args=parser.parse_args()
    pdfmetrics.registerFont(TTFont('CJK',args.font_regular,subfontIndex=0))
    pdfmetrics.registerFont(TTFont('CJK-Bold',args.font_bold,subfontIndex=0))
    pdfmetrics.registerFontFamily('CJK',normal='CJK',bold='CJK-Bold',italic='CJK',boldItalic='CJK-Bold')
    styles={
        'body':ParagraphStyle('body',fontName='CJK',fontSize=10.5,leading=16.4,textColor=INK,wordWrap='CJK',spaceAfter=8,alignment=TA_LEFT),
        'title':ParagraphStyle('title',fontName='CJK-Bold',fontSize=27,leading=35,textColor=INK,spaceAfter=7,wordWrap='CJK'),
        'h2':ParagraphStyle('h2',fontName='CJK-Bold',fontSize=19,leading=26,textColor=BLUE,spaceBefore=3,spaceAfter=15,wordWrap='CJK',keepWithNext=True),
        'h3':ParagraphStyle('h3',fontName='CJK-Bold',fontSize=12.2,leading=18,textColor=BLUE,spaceBefore=8,spaceAfter=6,wordWrap='CJK',keepWithNext=True),
        'table':ParagraphStyle('table',fontName='CJK',fontSize=9.2,leading=13.6,textColor=INK,wordWrap='CJK'),
        'tablehead':ParagraphStyle('tablehead',fontName='CJK-Bold',fontSize=9.5,leading=14,textColor=BLUE,wordWrap='CJK'),
        'caption':ParagraphStyle('caption',fontName='CJK',fontSize=8.3,leading=12,textColor=MUTED,spaceAfter=10,wordWrap='CJK'),
        'quote':ParagraphStyle('quote',fontName='CJK',fontSize=10.5,leading=17,textColor=INK,wordWrap='CJK',backColor=PALE,borderPadding=12,spaceBefore=8,spaceAfter=17,leftIndent=12,rightIndent=12),
    }
    source=ROOT/'docs/PROJECT_EXPLANATION.md'
    text=source.read_text()

    def inline(value):
        value=escape(value)
        def link(match):
            label,url=match.groups()
            if not url.startswith('https://'):
                rel=(source.parent/url).resolve().relative_to(ROOT)
                url='https://github.com/oNanachii/patchtrail/blob/main/'+rel.as_posix()
            return f'<link href="{url}" color="#225C87">{label}</link>'
        value=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,value)
        value=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',value)
        value=re.sub(r'`([^`]+)`',lambda m:f'<font name="Courier" size="9">{m.group(1)}</font>',value)
        return value

    story=[]
    lines=text.splitlines()
    i=0
    while i<len(lines):
        line=lines[i].strip()
        if not line:
            i+=1
            continue
        if line=='<!-- page -->':
            story.append(PageBreak());i+=1;continue
        if line.startswith('<!-- diagram:'):
            story.append(Diagram(line.split(':',1)[1].split(' ',1)[0]));story.append(Spacer(1,8));i+=1;continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cells=[x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?',x) for x in cells):rows.append(cells)
                i+=1
            count=len(rows[0]); widths=([135,170,WIDTH-305] if count==3 else [170,WIDTH-170])
            data=[[Paragraph(inline(x),styles['tablehead' if n==0 else 'table']) for x in row] for n,row in enumerate(rows)]
            table=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),PALE),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),0.7,LINE),('LINEBELOW',(0,1),(-1,-1),0.4,LINE),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
            story.extend([table,Spacer(1,12)]);continue
        image=re.fullmatch(r'!\[([^\]]+)\]\(([^)]+)\)',line)
        if image:
            caption,path=image.groups()
            path=(source.parent/path).resolve()
            im=Image(str(path));im.drawHeight*=WIDTH/im.drawWidth;im.drawWidth=WIDTH
            story.extend([im,Spacer(1,5),Paragraph(inline(caption),styles['caption'])]);i+=1;continue
        kind='body'
        if line.startswith('# '):kind='title';line=line[2:]
        elif line.startswith('## '):kind='h2';line=line[3:]
        elif line.startswith('### '):kind='h3';line=line[4:]
        elif line.startswith('> '):kind='quote';line=line[2:]
        elif line.startswith('- '):line='• '+line[2:]
        story.append(Paragraph(inline(line),styles[kind]));i+=1
    args.output.parent.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(args.output),pagesize=A4,rightMargin=44,leftMargin=44,topMargin=39,bottomMargin=57,title='PatchTrail项目讲解：从实际使用到面试表达',author='PatchTrail - AI协作项目说明',pageCompression=1)
    doc.build(story,canvasmaker=NumberedCanvas)
    print(args.output)


if __name__=='__main__':
    main()
