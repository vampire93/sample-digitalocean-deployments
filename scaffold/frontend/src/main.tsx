import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'

/** Last line of defence: a render crash shows a message, never a white page. */
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('Unhandled render error', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="state state-error" role="alert">
          <p className="state-title">The console hit an unexpected error.</p>
          <p className="state-hint mono">{this.state.error.message}</p>
          <button className="btn" onClick={() => location.reload()}>
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
