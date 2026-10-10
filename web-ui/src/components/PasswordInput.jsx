import { useState } from 'react'

export default function PasswordInput({ label, inputRef, id, disabled, ...inputProps }) {
  const [visible, setVisible] = useState(false)
  return <div className="va-password-field">
    <label htmlFor={id}>{label}</label>
    <div className="va-password-input">
      <input {...inputProps} ref={inputRef} id={id} className="va-login-input"
        type={visible ? 'text' : 'password'} disabled={disabled} />
      <button className="va-password-toggle" type="button" disabled={disabled}
        aria-label={visible ? '隐藏密码' : '显示密码'} aria-controls={id}
        onClick={() => setVisible(value => !value)}>{visible ? '隐藏' : '显示'}</button>
    </div>
  </div>
}
