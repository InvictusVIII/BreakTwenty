import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import TimelineCustomRangePicker from './TimelineCustomRangePicker';

function renderPicker(props = {}) {
  return render(
    <TimelineCustomRangePicker
      mode="single"
      startDate="2026-08-22"
      endDate="2026-08-22"
      onApply={vi.fn()}
      onCancel={vi.fn()}
      {...props}
    />
  );
}

describe('TimelineCustomRangePicker', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 7, 22, 12));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('only highlights the present month when viewing months for the present year', () => {
    renderPicker();

    fireEvent.click(screen.getByRole('button', { name: 'August 2026' }));

    const august = screen.getByRole('button', { name: 'Aug' });
    expect(august).toHaveClass('is-selected');

    const previousYear = screen.getByRole('button', { name: 'Previous year' });
    Array.from({ length: 6 }).forEach(() => fireEvent.click(previousYear));

    expect(screen.getByRole('button', { name: '2020' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Aug' })).not.toHaveClass('is-selected');
  });

  it('only highlights the present year in the year grid', () => {
    renderPicker();

    fireEvent.click(screen.getByRole('button', { name: 'August 2026' }));
    const previousYear = screen.getByRole('button', { name: 'Previous year' });
    Array.from({ length: 6 }).forEach(() => fireEvent.click(previousYear));
    fireEvent.click(screen.getByRole('button', { name: '2020' }));

    expect(screen.getByRole('button', { name: '2020' })).not.toHaveClass('is-selected');
    expect(screen.getByRole('button', { name: '2026' })).toHaveClass('is-selected');
  });
});
