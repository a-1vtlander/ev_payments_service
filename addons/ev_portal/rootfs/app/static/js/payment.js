/**
 * payment.js – Square Web Payments SDK: card form + Apple Pay.
 *
 * Expects window.PAYMENT_CONFIG to be set by the page before this script loads:
 *   window.PAYMENT_CONFIG = {
 *     appId:        "<square app id>",
 *     locationId:   "<square location id>",
 *     sessionUid:   "<one-time session uid>",
 *     submitUrl:    "<absolute url to POST /submit_payment>",
 *     amountCents:  <integer cents>,
 *     currencyCode: "USD"
 *   };
 */
(async () => {
  const { appId, locationId, sessionUid, submitUrl, amountCents, currencyCode } =
    window.PAYMENT_CONFIG || {};

  if (!window.Square) {
    document.getElementById('payment-status').textContent =
      'Square Payments SDK failed to load. Please refresh.';
    return;
  }

  const payments = window.Square.payments(appId, locationId);

  // ── Shared helpers ──────────────────────────────────────────────────────

  const escHtml = s =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  const btn    = document.getElementById('card-button');
  const status = document.getElementById('payment-status');

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

  /**
   * POST the token to /submit_payment then redirect on success.
   * @param {string} token  - Square nonce token
   * @param {string} givenName
   * @param {string} familyName
   * @param {function} onError  - called with msg string on non-success
   */
  const submitToken = async (token, givenName, familyName, onError) => {
    const fd = new FormData();
    fd.append('source_id',   token);
    fd.append('uid',         sessionUid);
    fd.append('given_name',  givenName);
    fd.append('family_name', familyName);

    let resp, result;
    try {
      resp = await fetch(submitUrl, { method: 'POST', body: fd });
    } catch (err) {
      onError('Network error: could not reach server. ' + err.message);
      return;
    }

    try {
      result = await resp.json();
    } catch (_) {
      const text = await resp.text().catch(() => 'no response body');
      onError('Server error (HTTP ' + resp.status + '): ' + text.slice(0, 200));
      return;
    }

    if (result.status === 'card_error') {
      onError(result.message || 'Card processing failed. Please try a different card.');
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

    onError('Unexpected response from server. Please try again.');
  };

  // ── Apple Pay ───────────────────────────────────────────────────────────

  const applePayContainer = document.getElementById('apple-pay-container');
  const applePayBtn       = document.getElementById('apple-pay-button');

  try {
    const paymentRequest = payments.paymentRequest({
      countryCode:  'US',
      currencyCode: currencyCode || 'USD',
      total: {
        amount: (amountCents / 100).toFixed(2),
        label:  'EV Charging Authorization',
      },
      requestBillingContact: { givenName: true, familyName: true },
    });

    // Will throw if Apple Pay is unavailable (non-Safari, no enrolled card, HTTP).
    const applePay = await payments.applePay(paymentRequest);

    applePayBtn.addEventListener('click', async () => {
      status.textContent = 'Opening Apple Pay…';

      let tokenResult;
      try {
        tokenResult = await applePay.tokenize();
      } catch (err) {
        showBanner('Apple Pay error: ' + err.message);
        status.textContent = '';
        return;
      }

      if (!tokenResult || tokenResult.status !== 'OK') {
        const errs = ((tokenResult && tokenResult.errors) || []).map(e => e.message).join(', ');
        showBanner(errs || 'Apple Pay was cancelled or failed.');
        status.textContent = '';
        return;
      }

      status.textContent = 'Processing Apple Pay…';

      const billing    = (tokenResult.details && tokenResult.details.billing) || {};
      const givenName  = billing.givenName  ||
                         document.getElementById('given-name').value.trim()  || 'Apple Pay';
      const familyName = billing.familyName ||
                         document.getElementById('family-name').value.trim() || 'Customer';

      await submitToken(tokenResult.token, givenName, familyName, msg => {
        showBanner(msg);
        status.textContent = '';
      });
    });

  } catch (_) {
    // Apple Pay unavailable — hide gracefully.
    if (applePayContainer) applePayContainer.style.display = 'none';
  }

  // ── Card form ───────────────────────────────────────────────────────────

  const card = await payments.card();
  await card.attach('#card-container');

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

    await submitToken(tokenResult.token, givenName, familyName, msg => {
      showBanner(msg);
      status.textContent = '';
      btn.disabled = false;
    });
  });
})();
