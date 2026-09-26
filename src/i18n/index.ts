import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

// English only. UI strings still live in locale files so components keep using t().
import commonEn from '../locales/en/common.json';
import dashboardEn from '../locales/en/dashboard.json';
import settingsEn from '../locales/en/settings.json';
import jobsEn from '../locales/en/jobs.json';
import applyEn from '../locales/en/apply.json';
import memoryEn from '../locales/en/memory.json';
import qaEn from '../locales/en/qa.json';
import logsEn from '../locales/en/logs.json';
import feedbackEn from '../locales/en/feedback.json';
import profileEn from '../locales/en/profile.json';
import llmEn from '../locales/en/llm.json';
import wizardEn from '../locales/en/wizard.json';
import guideEn from '../locales/en/guide.json';

const resources = {
  en: {
    common: commonEn,
    dashboard: dashboardEn,
    settings: settingsEn,
    jobs: jobsEn,
    apply: applyEn,
    memory: memoryEn,
    qa: qaEn,
    logs: logsEn,
    feedback: feedbackEn,
    profile: profileEn,
    llm: llmEn,
    wizard: wizardEn,
    guide: guideEn,
  },
};

i18n
  .use(initReactI18next)
  .init({
    resources,
    lng: 'en',
    fallbackLng: 'en',
    defaultNS: 'common',
    ns: ['common', 'dashboard', 'settings', 'jobs', 'apply', 'memory', 'qa', 'logs', 'feedback', 'profile', 'llm', 'wizard', 'guide'],
    interpolation: {
      escapeValue: false,
    },
    react: {
      useSuspense: false,
    },
  });

export default i18n;
