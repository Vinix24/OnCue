// Cloudflare Worker configuration for the waitlist landing page.
// Leave WORKER_URL empty to fall back to a mailto link; set it to your own
// deployed Worker endpoint (…workers.dev) to POST leads to /lead.
window.SALES_COPILOT_CONFIG = {
  WORKER_URL: "",
  UTM_SOURCE: "linkedin",
  UTM_MEDIUM: "post",
  UTM_CAMPAIGN: "v1-launch"
};
