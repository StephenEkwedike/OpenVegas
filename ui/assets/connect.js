import { authProbe, apiJson, getLoginHref } from "/ui/assets/page-auth.js?v=20260330";

const code = new URLSearchParams(location.search).get("code") || "";
const gameLogin = new URLSearchParams(location.search).get('mode') === 'game';
if(gameLogin)document.getElementById('connectionScope').textContent='This signs your terminal into your wallet and allows human-controlled games for 24 hours. You enter every wager yourself and can lose the stake. It does not authorize card charges, automatic play or AI spending. Deny if you did not start this connection.';
const status = document.getElementById("status");
const approve = document.getElementById("approve");
const deny = document.getElementById("deny");
const login = document.getElementById("login");
let submitting = false;

async function decide(approved) {
  if (submitting) return;
  submitting = true;
  approve.disabled = deny.disabled = true;
  try {
    await apiJson(gameLogin ? '/integrations/game-login/approve' : '/integrations/device/approve', {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_code: code, approved }),
    });
    status.textContent = approved
      ? "Approved. Return to your terminal to finish setup. You may close this tab."
      : "Denied. No terminal access was granted.";
  } catch (error) {
    status.textContent = error.status === 503
      ? "Native integrations are not enabled on this backend yet."
      : "Approval failed or expired. Return to your terminal and start a new connection.";
  }
}

if (!/^[A-F0-9]{12}$/.test(code)) {
  status.textContent = "Missing or invalid code. Run npx openvegas-ai go in your shell to get a fresh connection code.";
} else {
  document.getElementById("code").textContent = code.match(/.{4}/g).join(" ");
  approve.addEventListener("click", () => decide(true));
  deny.addEventListener("click", () => decide(false));
  try {
    const result = await authProbe();
    if (result.ok) {
      status.textContent = "Signed in. Compare the code before approving.";
      approve.disabled = deny.disabled = false;
    } else {
      status.textContent = "Sign in to your existing account, or sign up on the login page.";
      login.href = getLoginHref(location.pathname + location.search);
      login.hidden = false;
    }
  } catch {
    status.textContent = "Unable to check your account. Try reloading when the backend is available.";
  }
}
