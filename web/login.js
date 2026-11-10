/* The sign-in page: the password, or on first run the setup code from the panel's log plus a new password
   (app/auth.py), and "Sign in with <provider>" when OIDC is set up (app/oidc.py). On success it goes back to where
   the user was headed. */
"use strict";
(async function () {
  const $ = (s) => document.querySelector(s);
  const err = (t) => { $("#err").textContent = t || ""; };
  const next = () => {
    // Only back into this panel: a ?next= that resolves to another origin (//x, /\x, ...) goes home instead.
    let u;
    try { u = new URL(new URLSearchParams(location.search).get("next") || "/", location.origin); } catch { u = null; }
    location.href = u && u.origin === location.origin ? u.pathname + u.search + u.hash : "/";
  };
  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(((await r.json().catch(() => null)) || {}).detail || `failed (${r.status})`);
  }
  let st;
  try { st = await (await fetch("/api/auth/status", { cache: "no-store" })).json(); } catch { err("The panel is not answering."); return; }
  if (st.disabled || st.signed_in) return next();
  const params = new URLSearchParams(location.search);
  err(params.get("error"));                       // a refused OIDC sign-in comes back here with the reason
  if (st.oidc) {
    const go = "/api/auth/oidc/start?" + new URLSearchParams({ next: params.get("next") || "/" });
    const btn = $("#oidc");
    btn.textContent = `Sign in with ${st.oidc.name}`;
    btn.href = go;
    btn.hidden = false;
    if (st.oidc.auto_login && !params.has("error")) return location.assign(go);
  }
  if (st.password === false) return;               // PANEL_PASSWORD=off: the provider is the only way in
  const form = st.password_set ? $("#login") : $("#setup");
  form.hidden = false;
  form.querySelector("input").focus();
  $("#login").addEventListener("submit", async (e) => {
    e.preventDefault(); err();
    try { await post("/api/auth/login", { password: $("#pw").value }); next(); } catch (x) { err(x.message); }
  });
  $("#setup").addEventListener("submit", async (e) => {
    e.preventDefault(); err();
    if ($("#npw").value !== $("#npw2").value) return err("The two passwords differ.");
    if ($("#npw").value.length < st.min_password) return err(`Use at least ${st.min_password} characters.`);
    try { await post("/api/auth/setup", { code: $("#code").value, password: $("#npw").value }); next(); } catch (x) { err(x.message); }
  });
})();
