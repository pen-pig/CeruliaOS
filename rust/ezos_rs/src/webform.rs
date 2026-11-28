//! A deliberately small capability boundary: a page can compute strings and
//! edit its current search field, never invoke guest APIs or run unbounded code.
use super::{T_BUTTON, T_FIELDSET, T_FORM, T_INPUT, T_SELECT, T_TEXTAREA};

#[repr(C)]
pub struct Form {
    action: [u8; 256],
    name: [u8; 48],
    id: [u8; 64],
    value: [u8; 128],
    extras: [u8; 192],
    handler: [u8; 384],
    state: u8,
    scripts: u8,
    submitter: u8,
    required: u8,
    policy: u8,
    secret_name: [u8; 48],
    secret: [u8; 128],
    method: u8, // 0: GET; 1: POST
    secret_required: u8,
    plain_kind: u8,
}
const _: [(); 1256] = [(); core::mem::size_of::<Form>()];
type R<T> = Result<T, ()>;
fn z(a: &[u8]) -> &[u8] {
    &a[..a.iter().position(|&c| c == 0).unwrap_or(a.len())]
}
fn trim(mut a: &[u8]) -> &[u8] {
    while a.first().is_some_and(u8::is_ascii_whitespace) {
        a = &a[1..];
    }
    while a.last().is_some_and(u8::is_ascii_whitespace) {
        a = &a[..a.len() - 1];
    }
    a
}
fn append(dst: &mut [u8], pos: &mut usize, s: &[u8]) -> R<()> {
    if s.len() >= dst.len().saturating_sub(*pos) || s.contains(&0) {
        return Err(());
    }
    dst[*pos..*pos + s.len()].copy_from_slice(s);
    *pos += s.len();
    dst[*pos] = 0;
    Ok(())
}
fn set(dst: &mut [u8], s: &[u8]) -> R<()> {
    append(dst, &mut 0, s)
}
// Attribute slices borrow the HTML only during parsing. Never retain them in Form.
pub(super) fn attr<'a>(mut a: &'a [u8], name: &[u8]) -> Option<&'a [u8]> {
    while !a.is_empty() {
        let n = a
            .iter()
            .take_while(|&&c| !c.is_ascii_whitespace() && !b"/=><".contains(&c))
            .count();
        if n == 0 {
            a = &a[1..];
            continue;
        }
        let key = &a[..n];
        a = trim(&a[n..]);
        let mut v = &a[..0];
        if a.first() == Some(&b'=') {
            a = trim(&a[1..]);
            if let Some(&q @ (b'\'' | b'"')) = a.first() {
                a = &a[1..];
                let e = a.iter().position(|&c| c == q).unwrap_or(a.len());
                v = &a[..e];
                a = &a[(e + 1).min(a.len())..];
            } else {
                let e = a.iter().take_while(|c| !c.is_ascii_whitespace()).count();
                v = &a[..e];
                a = &a[e..];
            }
        }
        if key.eq_ignore_ascii_case(name) {
            return Some(v);
        }
    }
    None
}
fn decoded(out: &mut [u8], src: &[u8]) -> R<()> {
    let mut n = 0;
    let mut i = 0;
    while i < src.len() {
        if src[i] == b'&' {
            let mut tmp = [0; 8];
            let mut used = 0;
            let k = unsafe {
                super::decode_entity(
                    src.as_ptr(),
                    src.len() as u32,
                    i as u32,
                    tmp.as_mut_ptr(),
                    8,
                    &mut used,
                )
            };
            if k != 0 {
                append(out, &mut n, &tmp[..used as usize])?;
                i += k as usize;
                continue;
            }
        }
        append(out, &mut n, &src[i..i + 1])?;
        i += 1;
    }
    out[n] = 0;
    Ok(())
}
fn encode(dst: &mut [u8], n: &mut usize, src: &[u8]) -> R<()> {
    const HEX: &[u8] = b"0123456789ABCDEF";
    for &c in src {
        if c.is_ascii_alphanumeric() || b"*-._".contains(&c) {
            append(dst, n, &[c])?;
        } else if c == b' ' {
            append(dst, n, b"+")?;
        } else {
            append(
                dst,
                n,
                &[b'%', HEX[(c >> 4) as usize], HEX[(c & 15) as usize]],
            )?;
        }
    }
    Ok(())
}
// Only relax the image restriction when an explicit, unambiguous img-src
// permits ordinary network images. Do not interpret other source expressions,
// HTML character references, or a comma-separated list of policies here.
fn meta_allows_network_images(content: &[u8]) -> bool {
    if content.iter().any(|c| matches!(c, b'&' | b',' | 0)) { return false; }
    let mut allow = None;
    for directive in content.split(|&b| b==b';') {
        let mut words=directive.split(|b| b.is_ascii_whitespace()).filter(|w| !w.is_empty());
        let Some(name)=words.next() else { continue };
        if name.eq_ignore_ascii_case(b"upgrade-insecure-requests") ||
           name.eq_ignore_ascii_case(b"block-all-mixed-content") {return false}
        if name.eq_ignore_ascii_case(b"img-src") && allow.is_none() {
            let mut star=false;
            for w in words {
                if w==b"*" {star=true} else if !w.eq_ignore_ascii_case(b"data:") {return false}
            }
            allow=Some(star);
        }
    }
    allow==Some(true)
}
impl Form {
    pub(super) fn close(&mut self) {
        if self.state == 1 {
            self.state = if self.name[0] != 0 || self.secret_name[0]!=0 { 2 } else { 0 };
        } else if self.state == 3 {
            self.state = 0;
        }
    }
    pub(super) fn tag(&mut self, t: u8, a: &[u8]) {
        if t == super::T_META
            && attr(a, b"http-equiv")
                .is_some_and(|v| v.eq_ignore_ascii_case(b"content-security-policy"))
        {
            // Meta directives can contain entities. Do not attempt to interpret
            // them: disable all active content for the remainder of the page.
            self.policy |= 3;
            if !attr(a,b"content").is_some_and(meta_allows_network_images){self.policy|=4;}
            self.state = 0;
        }
        if self.policy & 2 != 0 {
            return;
        }
        if self.state == 2 {
            return;
        } // first complete supported form
        if t == T_FORM && self.state == 0 {
            // Keep in-place: this is a guest kernel with a small stack.
            self.name[0] = 0;
            self.id[0] = 0;
            self.value[0] = 0;
            self.extras[0] = 0;
            self.submitter = 0;
            self.required = 0;
            self.secret_name[0]=0;
            self.secret[0]=0;
            self.secret_required=0;
            self.plain_kind=0;
            let method = trim(attr(a, b"method").unwrap_or(b"get"));
            let get=method.eq_ignore_ascii_case(b"get");
            let post=method.eq_ignore_ascii_case(b"post") &&
                trim(attr(a,b"enctype").unwrap_or(b"application/x-www-form-urlencoded"))
                    .eq_ignore_ascii_case(b"application/x-www-form-urlencoded");
            self.method=post as u8;
            self.state = if (get||post)
                && decoded(&mut self.action, attr(a, b"action").unwrap_or(b"")).is_ok()
                && decoded(&mut self.handler, attr(a, b"onsubmit").unwrap_or(b"")).is_ok()
            {
                1
            } else {
                3
            };
        } else if self.state == 1 && self.control(t, a).is_err() {
            self.state = 3;
        }
    }
    fn control(&mut self, t: u8, a: &[u8]) -> R<()> {
        if t == T_FIELDSET && attr(a, b"disabled").is_some() {
            return Err(());
        }
        if ![T_INPUT, T_SELECT, T_TEXTAREA, T_BUTTON].contains(&t) || attr(a, b"disabled").is_some()
        {
            return Ok(());
        }
        if attr(a, b"form").is_some() {
            return Err(());
        }
        let k = attr(a, b"type").unwrap_or(if t == T_BUTTON { b"submit" } else { b"text" });
        let name = attr(a, b"name").unwrap_or(b"");
        let value = attr(a, b"value").unwrap_or(b"");
        let submit = k.eq_ignore_ascii_case(b"submit");
        if submit {
            if self.submitter != 0 {
                return Ok(());
            }
            self.submitter = 1;
            if [b"formaction".as_slice(), b"formmethod", b"formenctype", b"onclick"]
                .iter()
                .any(|n| attr(a, n).is_some())
            {
                return Err(());
            }
        }
        if name.is_empty() {
            return Ok(());
        }
        if t == T_INPUT && (k.eq_ignore_ascii_case(b"search") || k.eq_ignore_ascii_case(b"text") ||
                            (self.method==1&&k.eq_ignore_ascii_case(b"email"))) {
            if self.name[0] != 0 || attr(a, b"readonly").is_some() {
                return Err(());
            }
            decoded(&mut self.name, name)?;
            decoded(&mut self.value, value)?;
            self.plain_kind=k.eq_ignore_ascii_case(b"email") as u8;
            self.required = attr(a, b"required").is_some() as u8;
            decoded(&mut self.id, attr(a, b"id").unwrap_or(b""))
        } else if t==T_INPUT&&self.method==1&&k.eq_ignore_ascii_case(b"password") {
            if self.secret_name[0]!=0||attr(a,b"readonly").is_some(){return Err(());}
            decoded(&mut self.secret_name,name)?;
            self.secret_required=attr(a,b"required").is_some() as u8;
            // Never populate the password box from markup; only guest input.
            Ok(())
        } else if submit || (t == T_INPUT && k.eq_ignore_ascii_case(b"hidden")) {
            let mut key = [0; 48];
            let mut val = [0; 128];
            decoded(&mut key, name)?;
            decoded(&mut val, value)?;
            let mut n = z(&self.extras).len();
            append(&mut self.extras, &mut n, b"&")?;
            encode(&mut self.extras, &mut n, z(&key))?;
            append(&mut self.extras, &mut n, b"=")?;
            encode(&mut self.extras, &mut n, z(&val))
        } else {
            Err(())
        }
    }
    pub(super) fn script(&mut self, a: &[u8], src: &[u8]) {
        if self.policy != 0 || self.method!=0
            || self.name[0] == 0
            || !matches!(self.state, 1 | 2)
            || src.len() > 2048
            || self.scripts >= 16
            || attr(a, b"src").is_some()
            || attr(a, b"nomodule").is_some()
        {
            return;
        }
        let ty = attr(a, b"type").unwrap_or(b"");
        if !ty.is_empty()
            && !ty.eq_ignore_ascii_case(b"text/javascript")
            && !ty.eq_ignore_ascii_case(b"application/javascript")
        {
            return;
        }
        self.scripts += 1;
        let _ = eval(src, z(&self.id), &mut self.value, false);
    }
}
#[no_mangle]
pub unsafe extern "C" fn form_query_rs(f: &Form, out: *mut u8, cap: u32) -> i32 {
    if f.state != 2 || out.is_null() || cap == 0 {
        return -1;
    }
    let dst = core::slice::from_raw_parts_mut(out, cap as usize);
    let mut n = 0;
    let result = (|| -> R<()> {
        if f.name[0]!=0 {
            encode(dst,&mut n,z(&f.name))?;
            append(dst,&mut n,b"=")?;
            encode(dst,&mut n,z(&f.value))?;
        }
        if f.method==1&&f.secret_name[0]!=0 {
            if n>0{append(dst,&mut n,b"&")?;}
            encode(dst,&mut n,z(&f.secret_name))?;
            append(dst,&mut n,b"=")?;
            encode(dst,&mut n,z(&f.secret))?;
        }
        append(dst,&mut n,z(&f.extras))
    })();
    if result.is_ok() {
        n as i32
    } else {
        -1
    }
}
#[no_mangle]
pub extern "C" fn form_event_rs(f: &mut Form) -> i32 {
    if f.state != 2 {
        return -1;
    }
    if f.policy & 1 != 0 {
        return 1;
    } // CSP suppresses the inline event listener
    if f.method==1 { return if z(&f.handler).is_empty() {1} else {-1}; }
    match eval(z(&f.handler), z(&f.id), &mut f.value, true) {
        Ok(b) => b as i32,
        Err(()) => -1,
    }
}

// A bounded statement/expression evaluator, not pattern substitution.
// Unsupported syntax rolls back the entire script, including earlier assignments.
// No recursion, object lookup beyond the current field, network, or host callbacks.
struct Js<'a> {
    s: &'a [u8],
    id: &'a [u8],
    value: [u8; 128],
    vars: [(&'a [u8], [u8; 128]); 4],
    count: usize,
}
impl<'a> Js<'a> {
    fn take(&mut self, t: &[u8]) -> bool {
        self.s = trim(self.s);
        let Some(rest) = self.s.strip_prefix(t) else {
            return false;
        };
        if t.last().is_some_and(|c| c.is_ascii_alphanumeric())
            && rest
                .first()
                .is_some_and(|c| c.is_ascii_alphanumeric() || b"_$".contains(c))
        {
            return false;
        }
        self.s = rest;
        true
    }
    fn need(&mut self, t: &[u8]) -> R<()> {
        if self.take(t) {
            Ok(())
        } else {
            Err(())
        }
    }
    fn ident(&mut self) -> R<&'a [u8]> {
        self.s = trim(self.s);
        if !self
            .s
            .first()
            .is_some_and(|c| c.is_ascii_alphabetic() || b"_$".contains(c))
        {
            return Err(());
        }
        let n = self
            .s
            .iter()
            .take_while(|c| c.is_ascii_alphanumeric() || b"_$".contains(c))
            .count();
        let (id, rest) = self.s.split_at(n);
        self.s = rest;
        Ok(id)
    }
    fn string(&mut self) -> R<&'a [u8]> {
        self.s = trim(self.s);
        let q = *self.s.first().ok_or(())?;
        if q != b'\'' && q != b'"' {
            return Err(());
        }
        let rest = &self.s[1..];
        let n = rest.iter().position(|&c| c == q).ok_or(())?;
        if rest[..n].iter().any(|c| b"\\\r\n\0".contains(c)) {
            return Err(());
        }
        self.s = &rest[n + 1..];
        Ok(&rest[..n])
    }
    fn field(&mut self) -> R<()> {
        self.need(b".")?;
        self.need(b"getElementById")?;
        self.need(b"(")?;
        let id = self.string()?;
        if id.is_empty() || id != self.id {
            return Err(());
        }
        self.need(b")")?;
        self.need(b".")?;
        self.need(b"value")
    }
    fn expr(&mut self) -> R<[u8; 128]> {
        let mut result = [0; 128];
        let mut n = 0;
        loop {
            let mut v = [0; 128];
            if self.take(b"document") {
                self.field()?;
                v = self.value;
            } else if trim(self.s).first().is_some_and(|c| b"'\x22".contains(c)) {
                set(&mut v, self.string()?)?;
            } else {
                let id = self.ident()?;
                v = self.vars[..self.count]
                    .iter()
                    .find(|(k, _)| *k == id)
                    .ok_or(())?
                    .1;
            }
            while self.take(b".") {
                let op = self.ident()?;
                self.need(b"(")?;
                self.need(b")")?;
                match op {
                    b"trim" => {
                        let old = v;
                        set(&mut v, trim(z(&old)))?;
                    }
                    b"toUpperCase" if z(&v).is_ascii() => v.make_ascii_uppercase(),
                    b"toLowerCase" if z(&v).is_ascii() => v.make_ascii_lowercase(),
                    _ => return Err(()),
                }
            }
            append(&mut result, &mut n, z(&v))?;
            if !self.take(b"+") {
                return Ok(result);
            }
        }
    }
}
fn eval(src: &[u8], id: &[u8], value: &mut [u8; 128], handler: bool) -> R<bool> {
    let mut j = Js {
        s: src,
        id,
        value: *value,
        vars: [(b"", [0; 128]); 4],
        count: 0,
    };
    let mut allow = true;
    while !trim(j.s).is_empty() {
        if j.take(b";") {
            continue;
        }
        if j.take(b"return") {
            if !handler {
                return Err(());
            }
            if j.take(b"false") {
                allow = false;
            } else {
                j.need(b"true")?;
            }
            j.take(b";");
            if !trim(j.s).is_empty() {
                return Err(());
            }
            break;
        } else if j.take(b"document") {
            j.field()?;
            j.need(b"=")?;
            j.value = j.expr()?;
        } else if j.take(b"const") || j.take(b"let") || j.take(b"var") {
            let name = j.ident()?;
            if j.count == 4 || j.vars[..j.count].iter().any(|(k, _)| *k == name) {
                return Err(());
            }
            j.need(b"=")?;
            let v = j.expr()?;
            j.vars[j.count] = (name, v);
            j.count += 1;
        } else {
            return Err(());
        }
        if !trim(j.s).is_empty() {
            j.need(b";")?;
        }
    }
    *value = j.value;
    Ok(allow)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn field(handler: &[u8]) -> Form {
        // All members are u8. This is how the desktop initializes its C Form.
        let mut f: Form = unsafe { core::mem::zeroed() };
        f.tag(T_FORM, b"action=/find method=GET");
        f.tag(T_INPUT, b"id=q name=term type=search value=original");
        f.close();
        set(&mut f.handler, handler).unwrap();
        f
    }
    #[test]
    fn rollback_and_cancel() {
        let mut f = field(b"document.getElementById('q').value='changed';while(true){}");
        assert_eq!(form_event_rs(&mut f), -1);
        assert_eq!(z(&f.value), b"original");
        set(&mut f.handler, b"return false;").unwrap();
        assert_eq!(form_event_rs(&mut f), 0);
    }
    #[test]
    fn policy_and_inert_scripts() {
        assert!(meta_allows_network_images(b"default-src 'none'; IMG-SRC * data:"));
        for deny in [b"img-src 'none'; img-src *".as_slice(),b"img-src 'self'",b"default-src *",b"img-src * , img-src 'none'",b"img-src * &quot;"]{
            assert!(!meta_allows_network_images(deny));
        }
        let mut f = field(b"return false;");
        let js = b"document.getElementById('q').value='changed';";
        f.script(b"src='elsewhere'", js);
        f.script(b"type='application/json'", js);
        assert_eq!(z(&f.value), b"original");
        f.policy = 1;
        f.script(b"", js);
        assert_eq!(form_event_rs(&mut f), 1);
        assert_eq!(z(&f.value), b"original");
        f.tag(super::super::T_META, b"http-equiv=Content-Security-Policy");
        assert_ne!(f.state, 2);
    }
    #[test]
    fn unsupported_controls_are_not_submitted_as_partial_forms() {
        for tag in [b"name=second".as_slice(), b"type=password name=p"] {
            let mut f: Form = unsafe { core::mem::zeroed() };
            f.tag(T_FORM, b"");
            f.tag(T_INPUT, b"name=q");
            f.tag(T_INPUT, tag);
            f.close();
            assert_ne!(f.state, 2);
            f.tag(T_FORM, b"method=POST enctype=multipart/form-data");
            f.tag(T_INPUT, b"name=q");
            f.close();
            assert_ne!(f.state, 2);
            f.tag(T_FORM, b"method=GET");
            f.tag(T_INPUT, b"name=q");
            f.close();
            assert_eq!(f.state, 2);
        }
    }
    #[test]
    fn native_post_form_is_not_an_address_query_or_script_field() {
        let mut f: Form=unsafe{core::mem::zeroed()};
        f.tag(T_FORM,b"action=/login method=POST");
        f.tag(T_INPUT,b"type=hidden name=csrf value=a&amp;b");
        f.tag(T_INPUT,b"name=login type=email required");
        f.tag(T_INPUT,b"name=secret type=password value=fake required");
        f.close();
        assert_eq!(f.state,2);
        assert_eq!(f.method,1);
        assert_eq!(f.secret_required,1);
        assert_eq!(z(&f.secret),b"");
        set(&mut f.value,b"a+b@example.test").unwrap();
        set(&mut f.secret,b"x&y z").unwrap();
        f.script(b"",b"document.getElementById('').value='fake';");
        let mut out=[0;128];
        let n=unsafe{form_query_rs(&f,out.as_mut_ptr(),128)} as usize;
        assert_eq!(&out[..n],b"login=a%2Bb%40example.test&secret=x%26y+z&csrf=a%26b");
    }
    #[test]
    fn query_byte_boundaries() {
        let mut f = field(b"");
        set(&mut f.value, "中 +%".as_bytes()).unwrap();
        let mut out = [0; 128];
        let n = unsafe { form_query_rs(&f, out.as_mut_ptr(), 128) };
        assert_eq!(&out[..n as usize], b"term=%E4%B8%AD+%2B%25");
        assert_eq!(unsafe { form_query_rs(&f, out.as_mut_ptr(), n as u32) }, -1);
        assert_eq!(
            unsafe { form_query_rs(&f, out.as_mut_ptr(), (n + 1) as u32) },
            n
        );
    }
}
