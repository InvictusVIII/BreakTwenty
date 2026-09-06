import { useCallback, useState } from 'react';

function useTimelineCustomRangeDraft({ isOpen, committedKey, customKey }) {
  const [isDraftingCustomRange, setIsDraftingCustomRange] = useState(false);
  const isCustomCommitted = committedKey === customKey;
  const isCustomSelected = isCustomCommitted || isDraftingCustomRange;

  const openCustomRangeDraft = useCallback(() => {
    setIsDraftingCustomRange(true);
  }, []);

  const clearCustomRangeDraft = useCallback(() => {
    setIsDraftingCustomRange(false);
  }, []);

  return {
    isCustomCommitted,
    isCustomSelected,
    openCustomRangeDraft,
    clearCustomRangeDraft,
  };
}

export default useTimelineCustomRangeDraft;
