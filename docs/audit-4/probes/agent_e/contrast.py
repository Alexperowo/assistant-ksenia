def hx(h): h=h.lstrip('#'); return tuple(int(h[i:i+2],16) for i in (0,2,4))
def lin(c): c/=255; return c/12.92 if c<=0.04045 else ((c+0.055)/1.055)**2.4
def L(rgb): r,g,b=map(lin,rgb); return .2126*r+.7152*g+.0722*b
def over(fg,a,bg): return tuple(round(f*a+b*(1-a)) for f,b in zip(fg,bg))
def mix(a,b,t=.5): return tuple(round(x*(1-t)+y*t) for x,y in zip(a,b))
def cr(a,b): la,lb=sorted([L(a),L(b)],reverse=True); return (la+.05)/(lb+.05)
bg=hx('#070912'); fg=hx('#f5f7ff'); W=(255,255,255)
card=over(W,.06,bg); pillbg=over(W,.10,card)
rows=[
 ("fg on bg", fg, bg),
 ("muted on card", hx('#b9c0d8'), card),
 ("Да text on #059669 (left)", fg, hx('#059669')),
 ("Да text on mid", fg, mix(hx('#059669'),hx('#10b981'))),
 ("Да text on #10b981 (right)", fg, hx('#10b981')),
 ("Нет text on #be123c (left)", fg, hx('#be123c')),
 ("Нет text on mid", fg, mix(hx('#be123c'),hx('#fb7185'))),
 ("Нет text on #fb7185 (right)", fg, hx('#fb7185')),
 ("primary on #6d28d9", fg, hx('#6d28d9')),
 ("primary on #0e7490", fg, hx('#0e7490')),
 ("talk pressed on #0891b2", fg, hx('#0891b2')),
 ("'Ксения' label #8b5cf6 on card", hx('#8b5cf6'), card),
 ("'Ксения' label #22d3ee on card", hx('#22d3ee'), card),
 ("pill ok #34d399", hx('#34d399'), pillbg),
 ("pill warn #fbbf24", hx('#fbbf24'), pillbg),
 ("pill bad #fb7185", hx('#fb7185'), pillbg),
 ("tab muted on bar", hx('#b9c0d8'), over(hx('#070912'),.82,bg)),
 ("error #fb7185 on bg", hx('#fb7185'), bg),
 ("btn border .16 white vs bg (non-text)", over(W,.16,bg), bg),
 ("switch track off #2b3047 vs card (non-text)", hx('#2b3047'), card),
 ("disabled talk (opacity .45) text", over(fg,.45,over(hx('#6d28d9'),.45,bg)), over(hx('#6d28d9'),.45,bg)),
]
for n,a,b in rows: print(f"{cr(a,b):5.2f}  {n}   ({'#%02x%02x%02x'%a} on {'#%02x%02x%02x'%b})")
