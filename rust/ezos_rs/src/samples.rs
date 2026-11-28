//! Read-only Luogu problem-page adapter. Never evaluate script or interpret
//! sample contents as markup. Only selected by the final HTTPS problem URL.
use super::{HP, S_PRE};

#[derive(Clone, Copy)]
struct Cursor<'a>(&'a [u8]);
type R<T> = Result<T, ()>;
impl<'a> Cursor<'a> {
    fn pop(&mut self) -> R<u8> {
        let (&b, tail) = self.0.split_first().ok_or(())?;
        self.0 = tail;
        Ok(b)
    }
    fn ws(&mut self) {
        while self.0.first().is_some_and(|c| matches!(c, b' '|b'\t'|b'\n'|b'\r')) { let _ = self.pop(); }
    }
    fn take(&mut self, b:u8) -> bool {
        self.ws();
        if self.0.first() != Some(&b) { return false; }
        let _ = self.pop(); true
    }
    // Quoted token, returned without delimiters. Keep escapes until rendering.
    fn string(&mut self) -> R<&'a [u8]> {
        if !self.take(b'"') { return Err(()); }
        let src = self.0;
        loop {
            match self.pop()? {
                b'"' => return src.get(..src.len()-self.0.len()-1).ok_or(()),
                b'\\' => { self.pop()?; }
                0..=31 => return Err(()),
                _ => {}
            }
        }
    }
    // Bound the nesting of unrelated objects and skip their quoted contents.
    // No recursion on the kernel stack, even for a deeply nested document.
    fn span(&mut self) -> R<&'a [u8]> {
        self.ws();
        let src = self.0;
        match src.first().ok_or(())? {
            b'"' => { self.string()?; }
            b'[' | b'{' => {
                let (mut depth, mut types)=(0u32,0u32);
                loop {
                    if self.0.first()==Some(&b'"') { self.string()?; continue; }
                    match self.pop()? {
                        c @ (b'[' | b'{') => {
                            if depth==32 { return Err(()); }
                            types=(types<<1) | u32::from(c==b'{'); depth+=1;
                        }
                        c @ (b']' | b'}') => {
                            if types & 1 != u32::from(c==b'}') { return Err(()); }
                            depth-=1; types>>=1;
                            if depth==0 { break; }
                        }
                        _=>{}
                    }
                }
            }
            _ => {
                while self.0.first().is_some_and(|c| !b" \t\r\n,]}".contains(c)) { self.pop()?; }
                if self.0.len()==src.len() { return Err(()); }
            }
        }
        src.get(..src.len()-self.0.len()).ok_or(())
    }
    fn member(mut self, key:&[u8]) -> R<Self> {
        if !self.take(b'{') { return Err(()); }
        loop {
            let k=self.string()?;
            if !self.take(b':') { return Err(()); }
            let val=self.span()?;
            if k==key { return Ok(Self(val)); }
            if !self.take(b',') { return Err(()); }
        }
    }
    fn hex4(&mut self) -> R<u32> {
        let mut n=0;
        for _ in 0..4 { n=(n<<4) | (self.pop()? as char).to_digit(16).ok_or(())?; }
        Ok(n)
    }
}
unsafe fn render(h:&mut HP, string:&[u8]) -> R<()> {
    let mut c=Cursor(string);
    while !c.0.is_empty() {
        let b=c.pop()?;
        if b!=b'\\' { h.put(b); continue; }
        match c.pop()? {
            x @ (b'"'|b'\\'|b'/') => h.put(x),
            b'n'=>h.put(b'\n'), b'r'=>{ if !c.0.starts_with(b"\\n") { h.put(b'\n'); } },
            b't'=>h.put(b'\t'), b'b'|b'f'=>h.put(b' '),
            b'u'=>{
                let mut v=c.hex4()?;
                if (0xd800..=0xdbff).contains(&v) {
                    if c.pop()?!=b'\\' || c.pop()?!=b'u' { return Err(()); }
                    let low=c.hex4()?;
                    if !(0xdc00..=0xdfff).contains(&low) { return Err(()); }
                    v=0x10000+((v-0xd800)<<10)+(low-0xdc00);
                }
                let mut buf=[0;4];
                for &byte in char::from_u32(v).ok_or(())?.encode_utf8(&mut buf).as_bytes() { h.put(byte); }
            }
            _=>return Err(())
        }
    }
    Ok(())
}
unsafe fn pairs(h:&mut HP, mut c:Cursor<'_>) -> R<()> {
    if !c.take(b'[') { return Err(()); }
    if c.take(b']') { return Ok(()); }
    for i in 1..=64u8 {
        if !c.take(b'[') { return Err(()); }
        for label in ["样例输入 #","样例输出 #"] {
            h.style=super::S_BOLD;
            for &b in label.as_bytes() { h.put(b); }
            if i>=10 { h.put(b'0'+i/10); }
            h.put(b'0'+i%10); h.flush();
            h.style=S_PRE;
            render(h,c.string()?)?;
            h.flush();
            if label=="样例输入 #" && !c.take(b',') { return Err(()); }
        }
        if !c.take(b']') { return Err(()); }
        if c.take(b']') { return Ok(()); }
        if !c.take(b',') { return Err(()); }
    }
    Err(())
}
pub(super) unsafe fn append(h:&mut HP,json:&[u8]) {
    let Ok(samples)=Cursor(json).member(b"data").and_then(|c|c.member(b"problem")).and_then(|c|c.member(b"samples")) else {return};
    // Do not present partial output as a correct sample.
    let (nodes,text,old_trunc)=(h.ncount,h.tlen,h.truncated);
    if old_trunc!=0 { return; }
    h.href_off=0;
    if pairs(h,samples).is_err() || h.truncated!=0 {
        h.ncount=nodes; h.tlen=text; h.run_start=text;
    }
}
