import { ApplicationConfig, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideHttpClient, withFetch, withInterceptors } from '@angular/common/http';
import { csrfInterceptor } from './core/csrf.interceptor';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    // The CSRF interceptor because the session is a cookie, so a write has to prove it came from
    // this page. Reads are untouched: the token arrives on one.
    provideHttpClient(withFetch(), withInterceptors([csrfInterceptor])),
  ],
};
