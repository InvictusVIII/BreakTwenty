export function getRemoteDataViewState({ hasData, loading, error }) {
  if (!hasData && error) return 'error';
  // Effects only set `loading` after the component's first render. Treat the
  // pre-request state as loading too, so consumers never enter their ready
  // branch before the first response has supplied data.
  if (!hasData) return 'loading';
  return 'ready';
}
