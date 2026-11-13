import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'

// findBy*/waitFor default to 1 s. Under parallel load (13 jsdom workers, or the backend suite running beside) a multi-step async screen such as
// Onboarding can need longer, which made `Onboarding > interview` fail intermittently (DECISIONS M10): 5 s only changes how long a real failure takes.
configure({ asyncUtilTimeout: 5000 })
