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

  // ── Debug overlay: proxy console → on-page div when server sets debug_mode: true ──
  // Must be installed FIRST so every subsequent console call is captured.
  if (cfg.debugMode) {
    const _dbgBox  = document.getElementById('pay-debug');
    const _dbgMsgs = document.getElementById('pay-debug-msgs');
    if (_dbgBox && _dbgMsgs) {
      const _colours = { log: '#0f0', warn: '#ff0', error: '#f66' };
      ['log', 'warn', 'error'].forEach(method => {
        const _orig = console[method].bind(console);
        console[method] = (...args) => {
          _orig(...args);
          const text = args.map(a =>
            a instanceof Error    ? a.toString() :
            typeof a === 'object' && a !== null ? JSON.stringify(a) :
            String(a)
          ).join(' ');
          const line = document.createElement('div');
          line.style.color = _colours[method] || '#0f0';
          line.textContent = `[${method.toUpperCase()}] ${text}`;
          _dbgMsgs.appendChild(line);
          _dbgBox.style.display = '';
          _dbgBox.scrollTop = _dbgBox.scrollHeight;
        };
      });
    }
  }

  console.log(
    '[SDK] PAYMENT_CONFIG: appId=%s  locationId=%s  amountCents=%s  submitUrl=%s  sessionUid=%s  debugMode=%s',
    appId      || '(EMPTY)',
    locationId || '(EMPTY)',
    amountCents,
    submitUrl  || '(EMPTY)',
    sessionUid || '(EMPTY)',
    cfg.debugMode || false,
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

    console.log('[submitToken] POST %s  payment_method=%s  token_prefix=%s',
      submitUrl, paymentMethod, token ? token.slice(0, 8) : '(empty)');

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

  // Formats a Square SDK error into a multi-line detail string.
  // Square errors carry a .name (e.g. "UnexpectedError", "TokenizationError"),
  // a .message, and an .errors[] array where each entry has {type, message, detail}.
  const _fmtSqErr = (err) => {
    if (!err) return '(null)';
    const lines = [];
    lines.push('name:    ' + (err.name    || typeof err));
    lines.push('message: ' + (err.message || String(err)));
    if (err.errors && err.errors.length) {
      err.errors.forEach((e, i) => {
        lines.push(`errors[${i}]: type=${e.type || '?'}  message=${e.message || '?'}  detail=${e.detail || '?'}`);
      });
    }
    if (err.stack) {
      lines.push('stack:   ' + err.stack.split('\n').slice(0, 5).join(' → '));
    }
    return lines.join('\n');
  };

  // Write a block of text directly into the debug overlay (independent of
  // console proxying so it always appears even if the proxy wasn't set up).
  const _dbgWrite = (label, text) => {
    const box  = document.getElementById('pay-debug');
    const msgs = document.getElementById('pay-debug-msgs');
    if (!box || !msgs) return;
    const block = document.createElement('div');
    block.style.cssText = 'color:#f66;border-top:1px solid #333;margin-top:4px;padding-top:4px;white-space:pre-wrap;word-break:break-all';
    block.textContent = '[' + label + ']\n' + text;
    msgs.appendChild(block);
    box.style.display = '';
    box.scrollTop = box.scrollHeight;
  };

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
    // Race against a 10s timeout — the SDK can hang silently when the domain
    // association file is missing or unreachable, hiding the real error.
    const _applePayTimeout = new Promise((_, reject) =>
      setTimeout(() => reject(new Error(
        'payments.applePay() timed out (10 s) — ' +
        'check that /.well-known/apple-developer-merchantid-domain-association is reachable'
      )), 10000)
    );
    const applePay = await Promise.race([payments.applePay(paymentRequest), _applePayTimeout]);
    console.log('[ApplePay] available — showing button');

    // ── Reveal container ────────────────────────────────────────────────
    console.log('[ApplePay] container BEFORE: inline style.display="%s"  offsetWidth=%s  offsetHeight=%s',
      applePayContainer.style.display, applePayContainer.offsetWidth, applePayContainer.offsetHeight);

    applePayContainer.style.display = 'block';

    console.log('[ApplePay] container AFTER:  inline style.display="%s"  offsetWidth=%s  offsetHeight=%s',
      applePayContainer.style.display, applePayContainer.offsetWidth, applePayContainer.offsetHeight);

    const cs    = window.getComputedStyle(applePayContainer);
    const btnCs = window.getComputedStyle(applePayBtn);
    const rect  = applePayContainer.getBoundingClientRect();
    console.log('[ApplePay] container computed: display=%s  visibility=%s  opacity=%s  height=%s  overflow=%s',
      cs.display, cs.visibility, cs.opacity, cs.height, cs.overflow);
    console.log('[ApplePay] container rect: top=%s  left=%s  width=%s  height=%s  inViewport=%s',
      rect.top.toFixed(0), rect.left.toFixed(0), rect.width.toFixed(0), rect.height.toFixed(0),
      (rect.width > 0 && rect.height > 0));
    console.log('[ApplePay] button computed: display=%s  visibility=%s  height=%s  width=%s  appearance=%s',
      btnCs.display, btnCs.visibility, btnCs.height, btnCs.width,
      btnCs.webkitAppearance || btnCs.appearance || '(none)');

    // Walk ancestors and flag any that are hidden.
    let _node = applePayContainer.parentElement;
    while (_node && _node !== document.body) {
      const _cs = window.getComputedStyle(_node);
      if (_cs.display === 'none' || _cs.visibility === 'hidden' || _cs.opacity === '0') {
        console.warn('[ApplePay] HIDDEN ANCESTOR: <%s id="%s" class="%s">  display=%s  visibility=%s  opacity=%s',
          _node.tagName.toLowerCase(), _node.id, _node.className,
          _cs.display, _cs.visibility, _cs.opacity);
      }
      _node = _node.parentElement;
    }

    applePayBtn.addEventListener('click', async () => {
      try {
        status.textContent = 'Opening Apple Pay…';
        console.log('[ApplePay] tokenize: starting');

        let tokenResult;
        try {
          tokenResult = await applePay.tokenize();
          console.log('[ApplePay] tokenize result: status=%s', tokenResult && tokenResult.status);
        } catch (err) {
          const detail = _fmtSqErr(err);
          console.error('[ApplePay] tokenize threw:\n' + detail);
          if (cfg.debugMode) _dbgWrite('ApplePay tokenize error', detail);
          showBanner('Apple Pay error: ' + (err && (err.message || String(err))));
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

        const _rawMethod = (tokenResult.details && tokenResult.details.method) || '';
        console.log('[ApplePay] tokenize OK — raw method=%s  (will send as payment_method)', _rawMethod || '(empty — fallback: APPLE_PAY)');
        status.textContent = 'Processing Apple Pay…';

        const billing    = (tokenResult.details && tokenResult.details.billing) || {};
        const givenNameEl  = document.getElementById('given-name');
        const familyNameEl = document.getElementById('family-name');
        const givenName  = billing.givenName  || (givenNameEl  && givenNameEl.value.trim())  || 'Apple Pay';
        const familyName = billing.familyName || (familyNameEl && familyNameEl.value.trim()) || 'Customer';

        const method = _rawMethod || 'APPLE_PAY';
        await submitToken(tokenResult.token, givenName, familyName, msg => {
          console.error('[ApplePay] submitToken error:', msg);
          showBanner(msg);
          status.textContent = '';
        }, method);

      } catch (err) {
        // Catch-all: any unexpected throw inside the click handler — null refs,
        // SDK bugs, etc. — must surface rather than silently vanishing as an
        // unhandled promise rejection (invisible on mobile Safari).
        const detail = _fmtSqErr(err);
        console.error('[ApplePay] unexpected error in click handler:\n' + detail);
        if (cfg.debugMode) _dbgWrite('ApplePay click error', detail);
        showBanner('Apple Pay error: ' + (err && (err.message || String(err))));
        if (status) status.textContent = '';
      }
    });

  } catch (err) {
    // Apple Pay unavailable — container stays hidden (display:none set in CSS).
    // Common reasons: non-Safari browser, no enrolled card, served over HTTP,
    // Square SDK not loaded, domain association file missing or unreachable.
    const detail = _fmtSqErr(err);
    console.error('[ApplePay] init failed — button hidden.\n' + detail);
    if (cfg.debugMode) _dbgWrite('ApplePay init failed', detail);
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
