import { describe, expect, it } from 'vitest';
import { getRemoteDataViewState } from './remoteDataState';

describe('getRemoteDataViewState', () => {
  it('keeps first-load failures distinct from successful empty data', () => {
    expect(getRemoteDataViewState({ hasData: false, loading: true, error: '' })).toBe('loading');
    expect(getRemoteDataViewState({ hasData: false, loading: false, error: '' })).toBe('loading');
    expect(getRemoteDataViewState({ hasData: false, loading: false, error: 'Unavailable' })).toBe('error');
    expect(getRemoteDataViewState({ hasData: true, loading: false, error: '' })).toBe('ready');
  });

  it('keeps the last successful data visible when a refresh fails', () => {
    expect(getRemoteDataViewState({ hasData: true, loading: false, error: 'Refresh failed' }))
      .toBe('ready');
  });
});
