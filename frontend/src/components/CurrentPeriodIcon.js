import React from 'react';
import { LuCalendar } from 'react-icons/lu';

function CurrentPeriodIcon(props) {
  return (
    <LuCalendar
      aria-hidden="true"
      focusable="false"
      viewBox="-2 -2 28 28"
      {...props}
    />
  );
}

export default CurrentPeriodIcon;
