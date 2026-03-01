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
  const cfg = window.PAYMENT_CONFIG || {};
  const { appId, locationId, sessionUid, submitUrl, amountCents, currencyCode } = cfg;

  console.log(
    '[SDK] PAYMENT_CONFIG: appId=%s  locationId=%s  amountCents=%s  submitUrl=%s  sessionUid=%s',
    appId   || '(EMPTY)',
    locationId || '(EMPTY)',
    amountCents,
    submitUrl  || '(EMPTY)',
    sessionUid || '(EMPTY)',
  );

  if (!appId)      console.error('[SDK] appId is missing — Square SDK will not initialise');
  if (!locationId) console.error('[SDK] locationId is missing — Square SDK will not initialise');
  if (!submitUrl)  console.error('[SDK] submitUrl is missing — payment submission will fail');
  if (!sessionUid) console.error('[SDK] sessionUid is missing — payment submission will fail');

  if (!window.Square) {
    console.error('[SDK] window.Square is not defined — Square Web Payments SDK script failed to load');
    document.getElementById('payment-status').textContent =
      'Square Payments SDK failed to load. Please refresh.';
    return;
  }
  console.log('[SDK] window.Square loaded, version=%s', (window.Square.version || 'unknown'));

  let payments;
  try {
    payments = window.Square.payments(appId, locationId);
    console.log('[SDK] Square.payments() initialised OK');
  } catch (err) {
    console.error('[SDK] Square.payments() threw — appId or locationId likely invalid:', err);
    document.getElementById('payment-status').textContent =
      'Payment system failed to initialise. Contact support.';
    return;
  }

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
   * @param {string} token         - Square nonce token
   * @param {string} givenName
   * @param {string} familyName
   * @param {function} onError     - called with msg string on non-success
   * @param {string} paymentMethod - Square SDK details.method (CARD, APPLE_PAY, GOOGLE_PAY, ...)
   */
  const submitToken = async (token, givenName, familyName, onError, paymentMethod = 'CARD') => {
    const fd = new FormData();
    fd.append('source_id',      token);
    fd.append('uid',            sessionUid);
    fd.append('given_name',     givenName);
    fd.append('family_name',    familyName);
    fd.append('payment_method', paymentMethod);

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

  console.log('[ApplePay] init: protocol=%s  userAgent=%s', location.protocol, navigator.userAgent);

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
    console.log('[ApplePay] paymentRequest created: total=%s %s',
      (amountCents / 100).toFixed(2), currencyCode || 'USD');

    // Will throw if Apple Pay is unavailable (non-Safari, no enrolled card, HTTP).
    const applePay = await payments.applePay(paymentRequest);
    console.log('[ApplePay] available — showing button');

    // Only reveal the button now that we know Apple Pay is available.
    applePayContainer.style.display = '';

    applePayBtn.addEventListener('click', async () => {
      status.textContent = 'Opening Apple Pay…';
      console.log('[ApplePay] tokenize: starting');

      let tokenResult;
      try {
        tokenResult = await applePay.tokenize();
        console.log('[ApplePay] tokenize result: status=%s', tokenResult && tokenResult.status);
      } catch (err) {
        console.error('[ApplePay] tokenize threw:', err);
        showBanner('Apple Pay error: ' + err.message);
        status.textContent = '';
        return;
      }

      if (!tokenResult || tokenResult.status !== 'OK') {
        const errs = ((tokenResult && tokenResult.errors) || []).map(e => e.message).join(', ');
        console.warn('[ApplePay] tokenize failed: status=%s  errors=%s',
          tokenResult && tokenResult.status, errs || '(none)');
        showBanner(errs || 'Apple Pay was cancelled or failed.');
        status.textContent = '';
        return;
      }

      console.log('[ApplePay] tokenize OK — submitting token, method=%s',
        (tokenResult.details && tokenResult.details.method) || 'APPLE_PAY');
      status.textContent = 'Processing Apple Pay…';

      const billing    = (tokenResult.details && tokenResult.details.billing) || {};
      const givenName  = billing.givenName  ||
                         document.getElementById('given-name').value.trim()  || 'Apple Pay';
      const familyName = billing.familyName ||
                         document.getElementById('family-name').value.trim() || 'Customer';

      const method = (tokenResult.details && tokenResult.details.method) || 'APPLE_PAY';
      await submitToken(tokenResult.token, givenName, familyName, msg => {
        console.error('[ApplePay] submitToken error:', msg);
        showBanner(msg);
        status.textContent = '';
      }, method);
    });

  } catch (err) {
    // Apple Pay unavailable — container stays hidden (display:none set in CSS).
    // Common reasons: non-Safari browser, no enrolled card, served over HTTP,
    // Square SDK not loaded, domain association file missing.
    console.warn('[ApplePay] unavailable — button hidden. Reason:', err && (err.message || err));
  }

  // ── Card form ───────────────────────────────────────────────────────────

  let card;
  try {
    card = await payments.card();
    console.log('[Card] payments.card() created OK');
  } catch (err) {
    console.error('[Card] payments.card() threw:', err);
    document.getElementById('payment-status').textContent =
      'Card form failed to load. Please refresh the page.';
    return;
  }

  try {
    await card.attach('#card-container');
    console.log('[Card] card.attach() OK — form ready');
  } catch (err) {
    console.error('[Card] card.attach() threw:', err);
    document.getElementById('payment-status').textContent =
      'Card form failed to attach. Please refresh the page.';
    return;
  }

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
      console.log('[Card] tokenize result: status=%s', tokenResult && tokenResult.status);
    } catch (err) {
      console.error('[Card] tokenize threw:', err);
      showBanner('Card tokenization error: ' + err.message);
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    if (tokenResult.status !== 'OK') {
      const errs = (tokenResult.errors || []).map(e => e.message).join(', ');
      console.warn('[Card] tokenize failed: status=%s  errors=%s', tokenResult.status, errs || '(none)');
      showBanner('Card error: ' + errs);
      status.textContent = '';
      btn.disabled = false;
      return;
    }

    console.log('[Card] tokenize OK — submitting token');
    status.textContent = 'Processing...';

    await submitToken(tokenResult.token, givenName, familyName, msg => {
      console.error('[Card] submitToken error:', msg);
      showBanner(msg);
      status.textContent = '';
      btn.disabled = false;
    });
  });
})();
