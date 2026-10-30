#!/usr/bin/env python3
"""Build README images using real QEMU SSD1677 framebuffer screenshots."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root=Path(__file__).resolve().parents[1]/'assets'
shots=root/'screenshots'
ink='#161818'; paper='#F4F2EC'; dim='#686C69'; line='#D9D6CF'
fontdir='/System/Library/Fonts/Supplemental/'
def font(n,bold=False):return ImageFont.truetype(fontdir+('Arial Bold.ttf' if bold else 'Arial.ttf'),n)
def txt(d,s,x,y,n=24,color=ink,bold=False):d.text((x,y),s,font=font(n,bold),fill=color)
def screen(canvas,filename,x,y,height,angle=0):
    shot=Image.open(shots/filename).convert('RGB')
    width=round(height*480/800)
    frame=Image.new('RGBA',(width+45,height+65),(0,0,0,0))
    d=ImageDraw.Draw(frame)
    d.rounded_rectangle((2,2,width+42,height+62),radius=28,fill=ink)
    d.rounded_rectangle((10,10,width+34,height+55),radius=23,fill='#444747')
    frame.paste(shot.resize((width,height),Image.Resampling.NEAREST),(23,24))
    d.rounded_rectangle((width//2+8,height+39,width//2+38,height+42),radius=2,fill='#777')
    if angle:frame=frame.rotate(angle,expand=True,resample=Image.Resampling.BICUBIC)
    canvas.alpha_composite(frame,(x,y))

hero=Image.new('RGBA',(1920,780),paper);d=ImageDraw.Draw(hero)
for x in range(0,1920,48):d.line((x,0,x,780),fill='#ECEAE4')
for y in range(0,780,48):d.line((0,y,1920,y),fill='#ECEAE4')
d.rounded_rectangle((43,40,1877,740),radius=28,fill=paper,outline=line,width=3)
d.rounded_rectangle((95,90,176,170),radius=19,fill=ink)
txt(d,'Y',117,99,50,paper,True)
txt(d,'YBX  /  READER LAB',210,110,25,ink,True)
d.line((95,195,1035,195),fill=line,width=3)
for y,t in [(228,'ESP32'),(340,'SYSTEM'),(452,'EMULATOR')]:txt(d,t,91,y,105,ink,True)
txt(d,'Real firmware  /  real E-Ink pixels  /  no UI mockups',100,613,27,dim)
for x,w,t,fg,bg in [(100,193,'QEMU  /  S3',paper,ink),(307,223,'SSD1677  /  GT911','#47715A','#E1E8DF'),(544,203,'EXPERIMENTAL',dim,'#E9E7E1')]:
    d.rounded_rectangle((x,676,x+w,721),radius=22,fill=bg)
    txt(d,t,x+24,686,19,fg,True)
d.rounded_rectangle((1080,70,1845,709),radius=28,fill='#E7E5DF')
d.ellipse((1175,110,1790,710),outline='#C9C7BE',width=2)
screen(hero,'02-more.png',1120,145,425,8)
screen(hero,'03-ybx-settings.png',1480,83,495,-6)
hero.convert('RGB').save(root/'hero.png',optimize=True)

gallery=Image.new('RGB',(1700,760),paper);g=ImageDraw.Draw(gallery)
g.rounded_rectangle((20,20,1680,740),radius=27,outline=line,width=3)
items=[('01-home.png','HOME'),('02-more.png','MORE'),('03-ybx-settings.png','YBX SETTINGS'),('04-system-settings.png','SYSTEM SETTINGS')]
for i,(f,title) in enumerate(items):
    x=76+i*403
    g.rounded_rectangle((x,53,x+340,688),radius=19,fill='#FFFFFF',outline=line,width=2)
    txt(g,'0'+str(i+1),x+24,68,21,dim,True)
    img=Image.open(shots/f).convert('RGB').resize((276,460),Image.Resampling.NEAREST)
    gallery.paste(img,(x+32,122))
    g.line((x+30,604,x+309,604),fill=line,width=2)
    txt(g,title,x+30,628,21,ink,True)
txt(g,'FACTORY FIRMWARE FRAMES  /  SSD1677 PANEL RAM  /  NO HTML RECONSTRUCTIONS',90,715,16,dim)
gallery.save(root/'gallery.png',optimize=True)
print('Generated hero.png and gallery.png')
