(function () {
  const banner = document.getElementById('license-banner');
  const degradeBanner = document.getElementById('degrade-banner');
  const modal = document.getElementById('license-modal');
  const form = document.getElementById('license-form');

  if (!banner || !modal || !form) return;

  const openBtn = document.getElementById('open-license-modal');
  const dismissBtn = document.getElementById('dismiss-license-banner');
  const closeBtn = document.getElementById('close-license-modal');
  const successState = form.querySelector('.success-state');
  const errorState = form.querySelector('.error-state');
  const inputSection = form.querySelector('.input-section');

  const degradeDismissBtn = document.getElementById('dismiss-degrade-banner');
  const degradeCta = document.getElementById('degrade-upgrade-cta');

  const FOCUSABLE_SELECTOR =
    'a[href], button:not([disabled]), input:not([disabled]), textarea, select, [tabindex]:not([tabindex="-1"])';

  // PRO_UPGRADE_URL is defined once in constants.js (loaded before this file).
  const PRO_UPGRADE_URL = window.PRO_UPGRADE_URL;

  const openModal = () => {
    modal.classList.remove('hidden');
    modal.setAttribute('aria-modal', 'true');
    const first = modal.querySelector(FOCUSABLE_SELECTOR);
    if (first) first.focus();
  };

  const closeModal = () => {
    modal.classList.add('hidden');
    modal.removeAttribute('aria-modal');
    if (openBtn) openBtn.focus();
  };

  const safeJson = async (response) => {
    try {
      return await response.json();
    } catch (_e) {
      return null;
    }
  };

  const fetchLicenseStatus = async () => {
    try {
      const response = await fetch('/api/v1/license/status');
      if (!response.ok) return null;
      return safeJson(response);
    } catch (_e) {
      return null;
    }
  };

  const fetchLicenseFeatures = async () => {
    try {
      const response = await fetch('/api/v1/license/features');
      if (!response.ok) return null;
      return safeJson(response);
    } catch (_e) {
      return null;
    }
  };

  const showRegularBanner = (isExpired) => {
    banner.classList.remove('hidden');
    banner.classList.toggle('expired', isExpired);
    if (degradeBanner) degradeBanner.classList.add('hidden');
  };

  const showDegradeBanner = () => {
    if (degradeBanner) degradeBanner.classList.remove('hidden');
    banner.classList.add('hidden');
  };

  const hideBanners = () => {
    banner.classList.add('hidden');
    if (degradeBanner) degradeBanner.classList.add('hidden');
  };

  const applyLicenseState = async () => {
    const status = await fetchLicenseStatus();
    const features = await fetchLicenseFeatures();
    const tier = features && features.tier ? features.tier : 'free';

    if (tier !== 'free') {
      hideBanners();
      return;
    }

    const statusValue = status && status.status ? status.status : 'grace';
    if (statusValue === 'grace') {
      showRegularBanner(false);
    } else if (statusValue === 'expired' || statusValue === 'missing') {
      showDegradeBanner();
    } else {
      hideBanners();
    }
  };

  // Show banner when license_grace or license_expired WS event arrives
  window.addEventListener('license-grace-event', (e) => {
    const event = e.detail && e.detail.event ? e.detail.event : 'license_grace';
    if (event === 'license_grace') {
      showRegularBanner(false);
    } else {
      showDegradeBanner();
    }
  });

  if (openBtn) openBtn.addEventListener('click', openModal);
  if (dismissBtn) dismissBtn.addEventListener('click', () => banner.classList.add('hidden'));
  if (closeBtn) closeBtn.addEventListener('click', closeModal);

  if (degradeDismissBtn) {
    degradeDismissBtn.addEventListener('click', () => {
      if (degradeBanner) degradeBanner.classList.add('hidden');
    });
  }

  if (degradeCta) {
    degradeCta.href = PRO_UPGRADE_URL;
  }

  // Backdrop click closes modal
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  // Escape key closes modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
  });

  // Focus trap inside modal
  modal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const focusable = Array.from(modal.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
      (el) => !el.closest('.hidden')
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  // Form submit → POST /api/v1/license/request
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const emailInput = form.querySelector('input[name="email"]');
    const email = emailInput ? emailInput.value.trim() : '';
    if (errorState) errorState.classList.add('hidden');
    try {
      const resp = await fetch('/api/v1/license/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, source: 'dashboard' }),
      });
      if (resp.ok) {
        if (successState) successState.classList.remove('hidden');
        if (inputSection) inputSection.classList.add('hidden');
      } else {
        if (errorState) errorState.classList.remove('hidden');
      }
    } catch (_err) {
      if (errorState) errorState.classList.remove('hidden');
    }
  });

  applyLicenseState();
})();
