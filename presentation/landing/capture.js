'use strict';

(function () {
  var form = document.getElementById('waitlist-form');
  var statusEl = document.getElementById('form-status');
  var submitBtn = form ? form.querySelector('button[type="submit"]') : null;
  var extraToggle = document.getElementById('extra-toggle');
  var extraFields = document.getElementById('extra-fields');

  if (!form) return;

  var cfg = window.SALES_COPILOT_CONFIG || {};
  var workerUrl = cfg.WORKER_URL || '';

  // Optionele velden toggle
  if (extraToggle && extraFields) {
    extraToggle.addEventListener('click', function () {
      var open = extraFields.style.display !== 'none';
      extraFields.style.display = open ? 'none' : 'block';
      extraToggle.setAttribute('aria-expanded', String(!open));
    });
  }

  function setStatus(message, type) {
    statusEl.textContent = message;
    statusEl.className = 'form-status ' + type;
  }

  function clearStatus() {
    statusEl.textContent = '';
    statusEl.className = 'form-status';
  }

  function setLoading(loading) {
    submitBtn.disabled = loading;
    submitBtn.textContent = loading ? 'Bezig...' : 'Plaats me op de wachtlijst';
  }

  function fallbackMailto(email) {
    var subject = encodeURIComponent('OnCue waitlist');
    var body = encodeURIComponent('Ik wil op de wachtlijst voor OnCue Pro.\n\nE-mail: ' + email);
    var link = document.createElement('a');
    link.href = 'mailto:advies@vincentvandeth.nl?subject=' + subject + '&body=' + body;
    link.textContent = 'Stuur mij een e-mail';
    link.className = 'fallback-link';
    statusEl.innerHTML = '';
    statusEl.appendChild(document.createTextNode('Iets ging mis. '));
    statusEl.appendChild(link);
    statusEl.appendChild(document.createTextNode(' zodat je aanmelding aankomt.'));
    statusEl.className = 'form-status error';
  }

  function postLead(payload) {
    var endpoint = workerUrl.replace(/\/$/, '') + '/lead';
    return fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    clearStatus();

    var email = form.email.value.trim();
    var sector = form.sector ? form.sector.value : '';
    var company = form.company ? form.company.value.trim() : '';
    var useCase = form.use_case ? form.use_case.value.trim().slice(0, 100) : '';

    if (!email) {
      setStatus('Vul je e-mailadres in.', 'error');
      form.email.focus();
      return;
    }

    var emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailPattern.test(email)) {
      setStatus('Dit ziet er niet uit als een geldig e-mailadres.', 'error');
      form.email.focus();
      return;
    }

    if (!workerUrl || workerUrl.indexOf('REPLACE-WITH') !== -1) {
      // Config nog niet ingevuld — valt terug op mailto
      fallbackMailto(email);
      return;
    }

    setLoading(true);

    var payload = {
      email: email,
      source: cfg.UTM_SOURCE ? 'linkedin-landing' : 'landing',
      utm_source: cfg.UTM_SOURCE || null,
      utm_medium: cfg.UTM_MEDIUM || null,
      utm_campaign: cfg.UTM_CAMPAIGN || null,
      sector: sector || null,
      company: company || null,
      use_case: useCase || null
    };

    postLead(payload)
      .then(function (res) {
        return res
          .json()
          .catch(function () { return null; })
          .then(function (body) { return { res: res, body: body }; });
      })
      .then(function (result) {
        var res = result.res;
        var body = result.body;

        if (res.ok && body && body.status === 'ok') {
          setStatus('Bedankt! Ik neem binnen 48u contact op.', 'success');
          form.reset();
          if (extraFields) extraFields.style.display = 'none';
          if (extraToggle) extraToggle.setAttribute('aria-expanded', 'false');
          return;
        }
        if (res.ok && body && body.status === 'exists') {
          // Duplicate email — al op de wachtlijst
          setStatus('Je staat al op de wachtlijst, ik neem snel contact op.', 'success');
          form.reset();
          return;
        }
        // Andere fout (validatie, rate-limit, serverfout) — fallback mailto
        fallbackMailto(email);
      })
      .catch(function () {
        fallbackMailto(email);
      })
      .finally(function () {
        setLoading(false);
      });
  });
})();
