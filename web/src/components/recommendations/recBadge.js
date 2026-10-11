// Unseen recommendation count shared by the sidebar entry and the mobile chat header.
// A cheap COUNT endpoint, polled slowly and refreshed when the tab becomes visible.
import {reactive} from 'vue';
import {Api} from '../../api.js';

export const recommendationBadge = reactive({unseen: 0});
const POLL_MS = 90_000;
let timer = 0;
let started = false;
let inflight = null;

export function setRecommendationUnseen(value) {
  const number = Number(value);
  if (Number.isFinite(number) && number >= 0) recommendationBadge.unseen = Math.floor(number);
}

export function refreshRecommendationBadge() {
  if (inflight) return inflight;
  inflight = Api.recBadge()
    .then(data => setRecommendationUnseen(data?.unseen))
    .catch(() => { /* keep the last known count */ })
    .finally(() => { inflight = null; });
  return inflight;
}

export function startRecommendationBadge() {
  if (started || typeof window === 'undefined') return;
  started = true;
  void refreshRecommendationBadge();
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'hidden') void refreshRecommendationBadge();
  }, POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void refreshRecommendationBadge();
  });
}

export function stopRecommendationBadge() {
  window.clearInterval(timer);
  started = false;
}
