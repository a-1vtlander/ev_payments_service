/**
 * payment.js – Square Web Payments SDK card form logic.
 *
 * Expects window.PAYMENT_CONFIG to be set by the page before this script loads:
 *   window.PAYMENT_CONFIG = {
 *     appId:      "<square app id>",
 *     locationId: "<square location id>",
 *     sessionUid: "<one-time session uid>",
 *     submitUrl:  "<absolute url to POST /submit_payment>"
 *   };
 */
(async () => {
  const { appId, locationId, sessionUid, submitUrl } = window.PAYMENT_CONFIG || {};

  if (!window.Square) {
    document.getElementById('payment-status').textContent =
      'Square Payments SDK failed to load. Please refresh.';
    return;
  }

  const payments = window.Square.payments(appId, locationId);
  const card = await payments.card();
  await card.attach('#card-container');

  const escHtml = s =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  const infoCard = (label, val) =>
    '<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;' +
    'padding:1rem 1.2rem;margin-bottom:1rem;text-align:left">' +
    '<div style="font-size:.75rem;text-transform:uppercase;color:#777;margin-bottom:.2rem">' +
    label + '</div>' +
    '<div style="font-family:monospace;font-size:.9rem;word-break:break-all">' +
    val + '</div></div>';

  const showBanner = msg => {
    let el = document.querySelector('.error-banner');
    if (!el) {
      el = document.createElement('div');
      el.className = 'error-banner';
      btn.insertAdjacentElement('afterend', el);
    }
    el.textContent = '\u26a0  ' + msg;
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };

  const btn    = document.getElementById('card-button');
  const status = document.getElementById('payment-status');

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    status.textContent = 'Verifying card...';

    const givenName  = document.getElementById('given-name').value.trim();
    const familyName = document.getElementById('family-name').value.trim();
    if (!givenName || !familyName) {
      status.textContent = 'Please enter your first and last name.';
      btn.disabled = false;
      return;
    }

    let tokenResult;
    try {
      tokenResult = await card.tokenize();
    } catch (err) {
      showBanner('Card tokenization error: ' + err.message);
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    if (tokenResult.status !== 'OK') {
      const errs = (tokenResult.errors || []).map(e => e.message).join(', ');
      showBanner('Card error: ' + errs);
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    status.textContent = 'Processing...';

    const fd = new FormData();
    fd.append('source_id', tokenResult.token);
    fd.append('uid',         sessionUid);
    fd.append('given_name',  givenName);
    fd.append('family_name', familyName);

    let resp, result;
    try {
      resp = await fetch(submitUrl, { method: 'POST', body: fd });
    } catch (err) {
      showBanner('Network error: could not reach server. ' + err.message);
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    try {
      result = await resp.json();
    } catch (_) {
      const text = await resp.text().catch(() => 'no response body');
      showBanner('Server error (HTTP ' + resp.status + '): ' + text.slice(0, 200));
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    status.textContent = '';

    if (result.status === 'card_error') {
      showBanner(result.message || 'Card processing failed. Please try a different card.');
      btn.disabled = false;
      return;
    }

    if (result.status === 'error') {
      document.body.innerHTML =
        '<div style="font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 1rem">' +
        '<h2>\u274c Error</h2><p>' + escHtml(result.message) + '</p>' +
        '<p><a href="/">\u2190 Home</a></p></div>';
      return;
    }

    if (result.status === 'success') {
      window.location.href = result.session_url;
      return;
    }

    showBanner('Unexpected response from server. Please try again.');
    btn.disabled = false;
  });
})();
