/** @type {import('tailwindcss').Config} */
export default {
  // Ojo con esta lista: se heredó del prototipo, que era JavaScript. Sin `tsx` aquí,
  // Tailwind no genera NINGUNA utilidad para los componentes de este proyecto y la
  // pantalla sale en crudo, sin que falle ninguna compilación ni ningún test.
  content: ['./index.html', './src/**/*.{js,jsx,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        movistar: {
          blue: '#019DF4',
          celeste: '#73C8F9',
          magenta: '#E6007E',
          green: '#5BC500',
          gray: '#374151',
          alert: '#F59E0B',
        },
      },
      boxShadow: {
        card: '0 1px 3px 0 rgb(0 0 0 / 0.06), 0 1px 2px -1px rgb(0 0 0 / 0.06)',
      },
    },
  },
  // Sin preflight: el reset global de Tailwind pisaría los estilos propios de las
  // pantallas de WhatsApp y de la consola del asesor, que no usan utilidades.
  corePlugins: { preflight: false },
  plugins: [],
}
