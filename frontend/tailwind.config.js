/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{js,jsx}',
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: '#003087',
          50: '#e6eaf4',
          100: '#c0cbe3',
          200: '#97aad0',
          300: '#6d89bc',
          400: '#4d6fad',
          500: '#2e569e',
          600: '#264e96',
          700: '#1c448c',
          800: '#123b82',
          900: '#003087',
        },
        secondary: {
          DEFAULT: '#009cde',
          50: '#e0f4fc',
          100: '#b3e3f7',
          200: '#80d1f2',
          300: '#4dbfed',
          400: '#26b1e9',
          500: '#00a3e5',
          600: '#0097d8',
          700: '#0087c7',
          800: '#0078b7',
          900: '#009cde',
        },
        gold: {
          DEFAULT: '#ffc43a',
          50: '#fff8e6',
          100: '#ffedb9',
          200: '#ffe18c',
          300: '#ffd55f',
          400: '#ffcc3f',
          500: '#ffc43a',
          600: '#ffb833',
          700: '#ffaa2b',
          800: '#ff9d24',
          900: '#ff8716',
        },
      },
    },
  },
  plugins: [],
}
