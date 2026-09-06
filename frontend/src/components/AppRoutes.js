import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

function AppRoutes({ children }) {
  return (
    <Routes>
      {children}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default AppRoutes;
