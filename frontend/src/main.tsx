import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.new'
import DesktopWidget from './DesktopWidget'
import './styles.css'

const isWidget = new URLSearchParams(window.location.search).get('widget') === '1'
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>{isWidget ? <DesktopWidget /> : <App />}</React.StrictMode>,
)
