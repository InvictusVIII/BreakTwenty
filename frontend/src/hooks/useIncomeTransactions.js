import { useCallback, useEffect, useState } from 'react';
import { API } from '../config';
import { readTransactionCollectionResponse } from '../utils/apiResponse';

export default function useIncomeTransactions({ active, currency, resourceKey = '' }) {
  const [requestRevision, setRequestRevision] = useState(0);
  const [requestState, setRequestState] = useState({
    key: '',
    transactions: null,
    error: '',
  });
  const requestKey = `${currency}:${resourceKey}:${requestRevision}`;
  const ownsCurrentRequest = requestState.key === requestKey;
  const transactions = ownsCurrentRequest && Array.isArray(requestState.transactions)
    ? requestState.transactions
    : null;
  const error = ownsCurrentRequest ? requestState.error : '';
  const loading = active && !Array.isArray(transactions) && !error;

  useEffect(() => {
    if (!active || Array.isArray(transactions) || error) return undefined;

    let cancelled = false;
    const controller = new AbortController();
    const load = async () => {
      try {
        const response = await fetch(
          `${API}/transactions?limit=10000&convert_to=${encodeURIComponent(currency)}`,
          { signal: controller.signal },
        );
        const result = await readTransactionCollectionResponse(response, {
          label: 'Income transactions',
        });
        if (!cancelled) {
          setRequestState({ key: requestKey, transactions: result.transactions, error: '' });
        }
      } catch (requestError) {
        if (!cancelled && requestError?.name !== 'AbortError') {
          setRequestState({
            key: requestKey,
            transactions: null,
            error: requestError.message || 'Income transactions could not be loaded.',
          });
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [active, currency, error, requestKey, transactions]);

  const invalidate = useCallback(() => {
    setRequestRevision((value) => value + 1);
  }, []);

  return {
    incomeTransactions: transactions,
    incomeLoading: loading,
    incomeError: error,
    retryIncomeTransactions: invalidate,
  };
}
